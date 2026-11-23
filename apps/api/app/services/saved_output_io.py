"""Finite synchronous transport for registered saved-output owners."""

from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from yoao_assets_source.saved_io import SavedIOError

from app.services import storage_access
from app.services.content_snapshot import ContentError

_MISSING = frozenset({"404", "NoSuchKey", "NoSuchVersion", "NotFound"})
_PRECONDITIONS = frozenset({"409", "412", "PreconditionFailed", "ConditionalRequestConflict"})


def _missing(exc):
    return exc.status_code == 404 or exc.storage_code in _MISSING


def _error(kind, exc):
    if kind == "avatar":
        from app.services.avatar_storage import AvatarStorageError as error

        prefix = "avatar_storage_"
    elif kind == "bug":
        from app.services.bug_attachment_storage import BugAttachmentStorageError as error

        prefix = "bug_attachment_storage_"
    elif kind == "archive":
        from app.services.audit_archive_storage import AuditArchiveStorageError as error

        prefix = "audit_archive_storage_"
    elif kind == "zip":
        from app.services.export_storage import ExportStorageError as error

        prefix = "export_storage_"
    else:
        from app.services.export_media_storage import ExportMediaStorageError as error

        prefix = "export_media_storage_"
    if exc.unsettled or exc.status_code >= 500:
        suffix, status = "unavailable", 503
    elif _missing(exc):
        suffix, status = "missing", 404
    elif exc.status_code == 410:
        suffix, status = "expired", 409
    elif exc.code == "output_integrity_mismatch" or exc.storage_code in _PRECONDITIONS:
        suffix, status = ("invalid_receipt" if kind == "avatar" else "integrity_mismatch"), 409
    else:
        suffix, status = "identity_changed", 409
    result = error(prefix + suffix, status_code=status)
    return storage_access.mark_unsettled(result) if exc.unsettled else result


def _client(access, timeout):
    from app.services.saved_io import saved_io_client

    return saved_io_client(access, timeout=timeout)


def registered(target):
    return target is not None and getattr(target, "is_registered", False) is True


def capture(target, kind, receipt, *, relative=None, actor_id=None):
    if not registered(target):
        return None
    return SavedOutput(kind, receipt, relative=relative, actor_id=actor_id)


class SavedOutput:
    def __init__(self, kind, receipt, *, relative, actor_id):
        self.kind, self.receipt = kind, receipt
        self.operation = None
        ref = receipt.storage_ref
        self.location_id = UUID(ref["location_id"])
        self.key = ref.get("object_key")
        self.actor_id = actor_id
        self.timeout = 3600 if kind in {"media", "member"} else 600
        if kind == "avatar":
            self.proof = {"token": receipt.token}
        elif kind == "member":
            self.proof = {"bundle_id": str(receipt.id), "member_id": str(UUID(relative[:-4]))}
            self.key = ref["key_prefix"] + relative
            self.expected = receipt.inventory["objects"][self.key]
        else:
            name = {"bug": "upload_id", "zip": "output_id", "archive": "archive_id", "media": "bundle_id"}[kind]
            self.proof = {name: str(receipt.id)}
        if kind not in {"member", "media"}:
            self.expected = {"sha256": receipt.sha256, "size_bytes": receipt.size_bytes}

    def attach(self, permit):
        if self.operation is not None or permit.location_id != self.location_id:
            raise ContentError("storage_operation_changed")
        self.operation = permit.id

    def _proof(self):
        if self.operation is None:
            raise ContentError("storage_operation_changed")
        return {**self.proof, "operation_id": str(self.operation)}

    async def admit_read(self, db, actor_id, *, report_id=None, key=None):
        if not await db.scalar(
            text(
                "SELECT yoao_assets_v1.admit_saved_output_read("
                "CAST(:operation AS uuid), CAST(:family AS text), CAST(:id AS uuid), "
                "CAST(:actor AS uuid), CAST(:report AS uuid), CAST(:key AS text))"
            ),
            {
                "operation": self.operation, "family": self.kind, "id": self.receipt.id,
                "actor": actor_id, "report": report_id, "key": key,
            },
        ):
            raise ContentError("storage_operation_changed")

    def write(self, body, *, accounting=None):
        proof = self._proof()
        if self.kind == "bug":
            proof["actor_id"] = str(self.actor_id)
        attempt = accounting.begin(self.key, self.expected) if accounting is not None else None
        try:
            client = _client("write", self.timeout)
            try:
                result = client.upload(
                    "outputs", self.kind + "_write", proof, body, length=self.expected["size_bytes"]
                )
            except SavedIOError as exc:
                if not exc.unsettled and exc.code != "output_write_already_started":
                    raise
                result = client.command("outputs", "write_status", self._proof() | {"kind": self.kind})
            if (
                set(result) != {"etag", "version_id"}
                or not isinstance(result["etag"], str) or not 0 < len(result["etag"]) <= 1024
                or result["version_id"] is not None
                and (not isinstance(result["version_id"], str) or not 0 < len(result["version_id"]) <= 1024)
            ):
                raise SavedIOError("invalid_response", status_code=502, unsettled=True)
            version = result["version_id"]
            if attempt is not None:
                attempt.evidence.update(
                    physical_outcome="written", physical_size_bytes=self.expected["size_bytes"],
                    returned_version=version, full_readback=version not in {None, "null"},
                )
            return version
        except SavedIOError as exc:
            if attempt is not None and exc.storage_code in {"412", "PreconditionFailed"}:
                attempt.evidence.update(physical_outcome="not_written", current_delta_bytes=0)
            raise _error(self.kind, exc) from None

    def write_path(self, path, *, accounting=None):
        with storage_access.closing_registered(Path(path).open("rb")) as body:
            return self.write(body, accounting=accounting)

    def download_to(self, file):
        try:
            digest = sha256()
            client = _client("read", self.timeout)
            with client.download("outputs", self.kind + "_read", self._proof()) as response:
                if response.length != self.expected["size_bytes"]:
                    raise SavedIOError("output_integrity_mismatch")
                for chunk in response.iter_bytes():
                    digest.update(chunk)
                    file.write(chunk)
            if digest.hexdigest() != self.expected["sha256"]:
                raise SavedIOError("output_integrity_mismatch")
            file.seek(0)
        except SavedIOError as exc:
            raise _error(self.kind, exc) from None

    def read_bytes(self):
        with BytesIO() as file:
            self.download_to(file)
            return file.getvalue()

    def read_avatar(self):
        try:
            return self.read_bytes(), f'"{self.expected["sha256"]}"'
        except Exception as exc:
            if getattr(exc, "code", None) == "avatar_storage_missing" and not storage_access.failure_unsettled(exc):
                return None
            raise

    def verify(self, *, missing_ok=False):
        try:
            return _client("read", self.timeout).command(
                "outputs", self.kind + "_verify", self._proof()
            )["verified"]
        except SavedIOError as exc:
            if missing_ok and _missing(exc) and not exc.unsettled:
                return False
            raise _error(self.kind, exc) from None

    def sign(self, *, download_name=None):
        proof = self._proof()
        if download_name is not None:
            proof["download_name"] = download_name
        try:
            return _client("read", self.timeout).command("outputs", self.kind + "_sign", proof)["url"]
        except SavedIOError as exc:
            raise _error(self.kind, exc) from None

    def delete(self, *, accounting=None, version=None):
        if accounting is not None:
            accounting.begin(self.key, {"version_id": version})
        try:
            _client("cleanup", self.timeout).command("outputs", self.kind + "_delete", self._proof())
        except SavedIOError as exc:
            raise _error(self.kind, exc) from None

    def list_media(self):
        try:
            return _client("read", self.timeout).command("outputs", "media_list", self._proof())["verified"]
        except SavedIOError as exc:
            raise _error(self.kind, exc) from None

    def delete_media(self, *, accounting, versions):
        if accounting is not None:
            for key, version in versions.items():
                accounting.begin(key, {"version_id": version})
        try:
            return _client("cleanup", self.timeout).command("outputs", "media_delete", self._proof())["removed"]
        except SavedIOError as exc:
            raise _error(self.kind, exc) from None
