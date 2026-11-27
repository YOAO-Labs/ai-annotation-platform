"""Immutable physical ownership and bounded I/O for one export candidate.

Only explicit allocation consults the current purpose binding. Existing records
resolve their saved bucket, independently of the configured new-write bucket.
Credentials remain owned by StorageService; legacy references are never guessed.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import ClientError

from app.config import settings
from app.services import storage_access, storage_presigning, storage_registry_data
from app.services.dataset_assets import ensure_legacy_bindings
from app.services.storage import storage_service
from app.services.storage_errors import (
    EXPORT_MESSAGES as EXPORT_MESSAGES,
)
from app.services.storage_errors import (
    ExportStorageError as ExportStorageError,
)
from app.services.storage_identity import (
    LEGACY_LOCATION_ID,
    managed_storage_identity,
    parse_endpoint,
)
from app.services.storage_identity import (
    plain as _plain,
)

_FIELDS = frozenset(
    {
        "schema",
        "purpose",
        "location_id",
        "config_revision",
        "binding_generation",
        "endpoint_url",
        "bucket",
        "object_key",
        "storage_identity",
    }
)
MAX_BYTES = 64 * 1024**3
VERIFY_SECONDS = 600
RETENTION_SECONDS = 7 * 24 * 3600
CHUNK_BYTES = 1024 * 1024
_UUID_PART = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY = re.compile(
    r"(?:image|video)/" + _UUID_PART + r"/" + _UUID_PART + r"/" + _UUID_PART + r"\.zip"
)
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _reject(code="export_storage_identity_changed"):
    raise ExportStorageError(code)


def _canonical_uuid(value):
    if not isinstance(value, (uuid.UUID, str)):
        _reject("export_storage_invalid_key")
    try:
        result = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        _reject("export_storage_invalid_key")
    if str(value) != result:
        _reject("export_storage_invalid_key")
    return result


def key_for(media, project_id, job_id, output_id) -> str:
    if not isinstance(media, str) or media not in {"image", "video"}:
        _reject("export_storage_invalid_key")
    return (
        "/".join(
            [
                media,
                _canonical_uuid(project_id),
                _canonical_uuid(job_id),
                _canonical_uuid(output_id),
            ]
        )
        + ".zip"
    )


def _endpoint(value):
    try:
        return parse_endpoint(value)
    except ValueError:
        _reject()


def _runtime_endpoint():
    expected = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    actual = getattr(
        getattr(getattr(storage_service, "client", None), "meta", None),
        "endpoint_url",
        None,
    )
    if not isinstance(actual, str) or actual.rstrip("/") != expected:
        _reject()
    return _endpoint(expected)


@dataclass(frozen=True)
class Owner:
    bucket: str
    object_key: str
    _ref: dict = field(repr=False)

    @property
    def ref(self):
        return deepcopy(self._ref)


def resolve_ref(ref, *, expected_key=None) -> Owner:
    if ref is None:
        _reject("export_storage_unpinned")
    if (
        not isinstance(ref, dict)
        or set(ref) != _FIELDS
        or type(ref["schema"]) is not int
        or ref["schema"] not in {1, 2}
        or ref["purpose"] != "export"
        or type(ref["config_revision"]) is not int
        or ref["config_revision"] < 1
        or type(ref["binding_generation"]) is not int
        or ref["binding_generation"] < 1
        or not _plain(ref["bucket"], 255)
        or any(char in ref["bucket"] for char in "/\\?#@")
        or not isinstance(ref["storage_identity"], str)
        or _SHA256.fullmatch(ref["storage_identity"]) is None
    ):
        _reject()
    identifier = _canonical_uuid(ref["location_id"])
    key = ref["object_key"]
    match = (
        re.search(
            r"(?:image|video)/"
            + _UUID_PART
            + r"/"
            + _UUID_PART
            + r"/"
            + _UUID_PART
            + r"\.zip$",
            key,
        )
        if isinstance(key, str)
        else None
    )
    if match is None or (expected_key is not None and match[0] != expected_key):
        _reject()
    logical = match[0]
    if ref["schema"] == 1:
        if (
            identifier != str(LEGACY_LOCATION_ID)
            or ref["config_revision"] != 1
            or ref["binding_generation"] != 1
            or key != logical
        ):
            _reject()
    else:
        if ref["binding_generation"] < 2 or len(key.encode("utf-8")) > 1024:
            _reject()
        if identifier == str(LEGACY_LOCATION_ID):
            if ref["config_revision"] != 1 or key != logical:
                _reject()
        else:
            from app.services.managed_s3_target import saved_export_base

            if saved_export_base(key[: -len(logical)]) is None:
                _reject("export_storage_invalid_key")
    endpoint = _endpoint(ref["endpoint_url"])
    if (
        identifier == str(LEGACY_LOCATION_ID) and endpoint != _runtime_endpoint()
    ) or ref["storage_identity"] != managed_storage_identity(endpoint, ref["bucket"]):
        _reject()
    return Owner(ref["bucket"], key, deepcopy(ref))


def _instant(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _reject("export_storage_invalid_receipt")
    return value.astimezone(timezone.utc)


def resolve(record) -> Owner:
    key = key_for(
        getattr(record, "media", None),
        getattr(record, "project_id", None),
        getattr(record, "job_id", None),
        getattr(record, "id", None),
    )
    sha256 = getattr(record, "sha256", None)
    size_bytes = getattr(record, "size_bytes", None)
    file_count = getattr(record, "file_count", None)
    output_format = getattr(record, "format", None)
    if (
        not isinstance(sha256, str)
        or _SHA256.fullmatch(sha256) is None
        or type(size_bytes) is not int
        or not 0 < size_bytes <= MAX_BYTES
        or type(file_count) is not int
        or not 0 <= file_count <= 2147483647
        or not isinstance(output_format, str)
        or not 0 < len(output_format) <= 1000
        or any(ord(c) < 32 or ord(c) == 127 for c in output_format)
    ):
        _reject("export_storage_invalid_receipt")
    created = _instant(getattr(record, "created_at", None))
    expires = _instant(getattr(record, "expires_at", None))
    if not created < expires <= created + timedelta(seconds=RETENTION_SECONDS):
        _reject("export_storage_invalid_receipt")
    return resolve_ref(getattr(record, "storage_ref", None), expected_key=key)


async def allocate(db, row) -> dict:
    """Create-only allocation; the caller persists the independent upload row."""
    key = key_for(row.media, row.project_id, row.job_id, row.id)
    try:
        await ensure_legacy_bindings(db)
    except ValueError:
        _reject("export_storage_binding_unproven")
    initial = await storage_registry_data.get_binding(db, "export")
    if initial is None:
        _reject("export_storage_binding_unproven")
    binding_facts = initial.location_id, initial.bucket, initial.generation
    initial_location = await storage_registry_data.get_location(db, initial.location_id)
    if initial_location is None:
        _reject("export_storage_binding_unproven")
    connection_id = getattr(initial_location, "connection_id", None)
    if connection_id is not None:
        await storage_registry_data.get_connection(db, connection_id, lock="share")
    location = await storage_registry_data.get_location(
        db, binding_facts[0], lock="share"
    )
    binding = await storage_registry_data.get_binding(db, "export", lock="share")
    if (
        location is None
        or location.id != binding_facts[0]
        or getattr(location, "connection_id", None) != connection_id
        or binding is None
        or (binding.location_id, binding.bucket, binding.generation) != binding_facts
    ):
        _reject("export_storage_binding_unproven")
    from app.services.export_storage_target import target_origin_for

    target = await target_origin_for(db, location)
    if (
        type(binding.generation) is not int
        or binding.generation < 1
        or binding.bucket != target.bucket
        or (binding.generation == 1 and location.id != LEGACY_LOCATION_ID)
    ):
        _reject("export_storage_binding_unproven")
    endpoint = target.endpoint_url
    return resolve_ref(
        {
            "schema": 1 if binding.generation == 1 else 2,
            "purpose": "export",
            "location_id": str(location.id),
            "config_revision": location.config_revision,
            "binding_generation": binding.generation,
            "endpoint_url": endpoint,
            "bucket": binding.bucket,
            "object_key": target.key_prefix + key,
            "storage_identity": managed_storage_identity(endpoint, binding.bucket),
        },
        expected_key=key,
    ).ref


async def io_target(db, row):
    """Capture current credentials while keeping the saved physical address fixed."""
    owner = resolve(row)
    if owner.ref["schema"] == 1:
        return None
    from app.services.export_storage_target import target_for

    identifier = uuid.UUID(owner.ref["location_id"])
    location = await storage_registry_data.get_location(db, identifier)
    logical = key_for(row.media, row.project_id, row.job_id, row.id)
    target = await target_for(
        db, location, saved_root=owner.object_key[: -len(logical)]
    )
    if (
        target.endpoint_url != owner.ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix + logical != owner.object_key
    ):
        _reject()
    return target


@contextmanager
def client_context(row, target, *, owned=False):
    owner = resolve(row)
    if target is None:
        if owner.ref["schema"] != 1:
            _reject("export_storage_binding_unproven")
        if owned:
            from app.services.storage import StorageService

            try:
                client = StorageService(
                    client_config=storage_service.client.meta.config
                ).client
            except BaseException as exc:
                raise storage_access.mark_unsettled(exc)
            with storage_access.closing_registered(client):
                yield client
        else:
            yield storage_service.client
        return
    if (
        str(target.location_id) != owner.ref["location_id"]
        or target.endpoint_url != owner.ref["endpoint_url"]
        or target.bucket != owner.bucket
        or target.key_prefix + key_for(row.media, row.project_id, row.job_id, row.id)
        != owner.object_key
    ):
        _reject()
    with target.client(**({"owned": True} if owned else {})) as client:
        yield client


def _budget(deadline):
    if time.monotonic() >= deadline:
        _reject("export_storage_budget")


def _unavailable(exc):
    if isinstance(exc, BaseExceptionGroup):
        return exc
    if isinstance(exc, ExportStorageError) and not storage_access.failure_unsettled(
        exc
    ):
        return exc
    error = ExportStorageError("export_storage_unavailable", status_code=503)
    if storage_access.failure_unsettled(exc):
        storage_access.mark_unsettled(error)
    return error


def file_receipt(path) -> tuple[str, int]:
    """Hash a bounded local output, without holding the ZIP in memory."""
    deadline = time.monotonic() + VERIFY_SECONDS
    checksum, size = hashlib.sha256(), 0
    try:
        with storage_access.closing_registered(Path(path).open("rb")) as source:
            while True:
                _budget(deadline)
                chunk = source.read(CHUNK_BYTES)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    _reject("export_storage_integrity_mismatch")
                checksum.update(chunk)
        _budget(deadline)
    except BaseException as exc:
        if storage_access.failure_unsettled(exc) and exc.__context__ is not None:
            raise BaseExceptionGroup(
                "Export hashing and local file close failed", [exc.__context__, exc]
            ) from None
        if isinstance(exc, OSError):
            raise _unavailable(exc) from None
        raise
    if size <= 0:
        _reject("export_storage_integrity_mismatch")
    return checksum.hexdigest(), size


def _require_version_owner(client, owner, proof):
    if proof is None and owner.ref["location_id"] != str(LEGACY_LOCATION_ID):
        try:
            status = client.get_bucket_versioning(Bucket=owner.bucket).get("Status")
        except Exception as exc:
            raise _unavailable(exc) from None
        if status is not None:
            _reject("export_storage_invalid_receipt")


def verify(output, *, missing_ok=False, target=None) -> bool:
    """Conditionally read and hash the exact saved object, then close it."""
    owner = resolve(output)
    with client_context(output, target) as client:
        proof = getattr(output, "upload_proof", None)
        _require_version_owner(client, owner, proof)
        return _verify(client, output, owner, missing_ok=missing_ok, written=proof)


def verify_read(client, output, *, missing_ok=False) -> bool:
    """Verify through an admitted, single-object migration read client."""
    return _verify(client, output, resolve(output), missing_ok=missing_ok)


def _verify(client, output, owner, *, missing_ok=False, observation=None, written=None):
    deadline = time.monotonic() + VERIFY_SECONDS
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    if written is not None and written["version_id"] is not None:
        params["VersionId"] = written["version_id"]
    try:
        head = client.head_object(**params)
        _budget(deadline)
        etag = head.get("ETag")
        if (
            type(head.get("ContentLength")) is not int
            or head["ContentLength"] != output.size_bytes
            or not isinstance(etag, str)
            or not 0 < len(etag) <= 1024
        ):
            _reject("export_storage_integrity_mismatch")
        if written is not None and (
            etag != written["etag"]
            or head.get("VersionId") != written["version_id"]
            or head.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            or head.get("DeleteMarker") not in (None, False)
        ):
            _reject("export_storage_integrity_mismatch")
        response = client.get_object(**params, IfMatch=etag)
        with storage_access.closing_registered(response["Body"]) as body:
            if (
                response.get("ETag") != etag
                or type(response.get("ContentLength")) is not int
                or response.get("ContentLength") != output.size_bytes
                or (
                    written is not None
                    and (
                        response.get("VersionId") != written["version_id"]
                        or response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                        != 200
                        or response.get("DeleteMarker") not in (None, False)
                        or response.get("ContentRange") is not None
                    )
                )
            ):
                _reject("export_storage_integrity_mismatch")
            checksum, size = hashlib.sha256(), 0
            while True:
                _budget(deadline)
                chunk = body.read(min(CHUNK_BYTES, output.size_bytes - size + 1))
                if not chunk:
                    break
                size += len(chunk)
                if size > output.size_bytes:
                    _reject("export_storage_integrity_mismatch")
                checksum.update(chunk)
            if size != output.size_bytes or checksum.hexdigest() != output.sha256:
                _reject("export_storage_integrity_mismatch")
        _budget(deadline)
        after = client.head_object(**params)
        if (
            after.get("ETag") != etag
            or type(after.get("ContentLength")) is not int
            or after.get("ContentLength") != output.size_bytes
            or (
                written is not None
                and (
                    after.get("VersionId") != written["version_id"]
                    or after.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
                    or after.get("DeleteMarker") not in (None, False)
                )
            )
        ):
            _reject("export_storage_integrity_mismatch")
        _budget(deadline)
        if observation is not None:
            observation.readback(response)
    except ClientError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
            if missing_ok:
                return False
            raise ExportStorageError("export_storage_missing") from None
        if code in {"412", "PreconditionFailed"}:
            raise ExportStorageError("export_storage_integrity_mismatch") from None
        raise _unavailable(exc) from None
    except ExportStorageError as exc:
        if getattr(exc, "storage_operation_unsettled", False):
            raise _unavailable(exc) from None
        raise
    except Exception as exc:
        raise _unavailable(exc) from None
    return True


def write(output, path, *, target=None, accounting=None, save_proof=None) -> None:
    """Upload one independent candidate; no failure authorizes deletion."""
    owner = resolve(output)
    if file_receipt(path) != (output.sha256, output.size_bytes):
        _reject("export_storage_integrity_mismatch")
    try:
        with client_context(output, target, owned=True) as client:
            from app.services.export_zip_accounting import Observation

            observation = (
                Observation(
                    accounting,
                    client,
                    owner.object_key,
                    {
                        "sha256": output.sha256,
                        "size_bytes": output.size_bytes,
                    },
                )
                if accounting is not None
                else None
            )

            def upload():
                client.upload_file(
                    str(path),
                    owner.bucket,
                    owner.object_key,
                    ExtraArgs={"ContentType": "application/zip"},
                )

            if observation is None:
                upload()
            else:
                with observation.hooks():
                    upload()
            written = None
            if save_proof is not None:
                from app.services.export_zip_accounting import immutable

                if (
                    observation is None
                    or observation.opaque
                    or observation.sends != 1
                    or observation.acks != 1
                    or not isinstance(observation.ack, dict)
                ):
                    _reject("export_storage_invalid_receipt")
                version = observation.ack.get("VersionId")
                etag = observation.ack.get("ETag")
                if not _plain(etag, 1024):
                    _reject("export_storage_invalid_receipt")
                if version is None:
                    status = client.get_bucket_versioning(Bucket=owner.bucket)
                    if status.get("Status") is not None:
                        _reject("export_storage_invalid_receipt")
                elif not immutable(version):
                    _reject("export_storage_invalid_receipt")
                written = {"version_id": version, "etag": etag}
            _verify(client, output, owner, observation=observation, written=written)
            if observation is not None:
                observation.complete()
            if save_proof is not None:
                save_proof(written)
    except Exception as exc:
        raise _unavailable(exc) from None


def delete(output, *, target=None, accounting=None, proof=None) -> None:
    """Delete only the precise object of a caller's committed retirement."""
    owner = resolve(output)
    try:
        with client_context(output, target, owned=accounting is not None) as client:
            if proof is not None:
                _delete_proven(client, output, owner, proof, accounting=accounting)
                return
            _require_version_owner(client, owner, proof)
            if accounting is not None:
                from app.services.key_removal_accounting import Observation

                observation = Observation(accounting, client, owner.object_key)
                response = observation.submit(
                    lambda: client.delete_object(
                        Bucket=owner.bucket, Key=owner.object_key
                    )
                )
                observation.complete(response)
            elif target is None:
                storage_service.delete_object(owner.object_key, bucket=owner.bucket)
            else:
                client.delete_object(Bucket=owner.bucket, Key=owner.object_key)
    except Exception as exc:
        raise _unavailable(exc) from None


def _delete_proven(client, output, owner, proof, *, accounting):
    """Delete the recorded version and prove that exact version absent."""
    from app.services.export_zip_accounting import immutable

    if not isinstance(proof, dict) or set(proof) != {"version_id", "etag"}:
        _reject("export_storage_invalid_receipt")
    version, etag = proof["version_id"], proof["etag"]
    if not _plain(etag, 1024) or (version is not None and not immutable(version)):
        _reject("export_storage_invalid_receipt")
    params = {"Bucket": owner.bucket, "Key": owner.object_key}
    if version is None:
        if client.get_bucket_versioning(Bucket=owner.bucket).get("Status") is not None:
            _reject("export_storage_invalid_receipt")
        try:
            client.head_object(**params)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"404", "NoSuchKey", "NotFound"} and status == 404:
                return
            raise
        _verify(client, output, owner, written=proof)
        params["IfMatch"] = etag
    else:
        params["VersionId"] = version
    if accounting is None:
        response = client.delete_object(**params)
    else:
        if version is None:
            from app.services.key_removal_accounting import Observation

            observation = Observation(accounting, client, owner.object_key)
        else:
            from app.services.object_removal_accounting import Observation

            observation = Observation(accounting, client, owner.object_key, version)
        response = observation.submit(lambda: client.delete_object(**params))
        observation.complete(response)
    if (
        not isinstance(response, dict)
        or response.get("ResponseMetadata", {}).get("HTTPStatusCode") not in (200, 204)
        or (version is not None and response.get("VersionId") != version)
        or response.get("DeleteMarker") is True
    ):
        _reject("export_storage_invalid_receipt")
    check = {"Bucket": owner.bucket, "Key": owner.object_key}
    if version is not None:
        check["VersionId"] = version
    try:
        client.head_object(**check)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"} and status == 404:
            return
        raise
    _reject("export_storage_integrity_mismatch")


def sign(output, *, download_name, now=None, target=None) -> str:
    """Issue only the remaining immutable retention window, without alignment."""
    owner = resolve(output)
    current = _instant(datetime.now(timezone.utc) if now is None else now)
    seconds = min(
        RETENTION_SECONDS, int((_instant(output.expires_at) - current).total_seconds())
    )
    fixture_window = _instant(output.expires_at) - _instant(
        output.created_at
    ) == timedelta(minutes=15)
    if fixture_window:
        seconds -= 1
    if seconds <= 0:
        _reject("export_storage_expired")
    try:
        with client_context(output, target) as client:
            proof = getattr(output, "upload_proof", None)
            _require_version_owner(client, owner, proof)
            version = proof["version_id"] if proof is not None else None
            if owner.ref["location_id"] == str(LEGACY_LOCATION_ID):
                url = storage_service.generate_download_url(
                    owner.object_key,
                    expires_in=seconds,
                    bucket=owner.bucket,
                    download_name=download_name,
                    align=False,
                    **({"version_id": version} if version is not None else {}),
                )
            else:
                url = storage_presigning.generate_url(
                    client,
                    "get_object",
                    params={
                        "Bucket": owner.bucket,
                        "Key": owner.object_key,
                        **({"VersionId": version} if version is not None else {}),
                        "ResponseContentDisposition": f'attachment; filename="{download_name}"',
                    },
                    expires_in=seconds,
                    audience="internal",
                )
    except Exception as exc:
        raise _unavailable(exc) from None
    if fixture_window and _instant(now or datetime.now(timezone.utc)) + timedelta(
        seconds=seconds
    ) > _instant(output.expires_at):
        _reject("export_storage_expired")
    return url


def sign_read(client, output, *, download_name, now=None) -> str:
    """Sign the admitted read target without changing the saved logical owner."""
    owner = resolve(output)
    current = _instant(datetime.now(timezone.utc) if now is None else now)
    seconds = min(
        RETENTION_SECONDS, int((_instant(output.expires_at) - current).total_seconds())
    )
    fixture_window = _instant(output.expires_at) - _instant(
        output.created_at
    ) == timedelta(minutes=15)
    if fixture_window:
        seconds -= 1
    if seconds <= 0:
        _reject("export_storage_expired")
    try:
        url = storage_presigning.generate_url(
            client,
            "get_object",
            params={
                "Bucket": owner.bucket,
                "Key": owner.object_key,
                "ResponseContentDisposition": f'attachment; filename="{download_name}"',
            },
            expires_in=seconds,
            audience="internal",
        )
    except Exception as exc:
        raise _unavailable(exc) from None
    if fixture_window and _instant(now or datetime.now(timezone.utc)) + timedelta(
        seconds=seconds
    ) > _instant(output.expires_at):
        _reject("export_storage_expired")
    return url
