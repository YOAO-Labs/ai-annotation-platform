"""Physical operations for committed Learning versions, attempts and retirement."""

from hashlib import file_digest, sha256
from os import fstat
import re
from uuid import UUID

from yoao_assets_source.saved_io import SavedIOError

from .saved_io import Download

_CAPTURE = "yoao_assets_v1.capture_training_saved_io"
_RECORD = "yoao_assets_v1.record_training_saved_io"
_SCOPE = {"dataset", "run", "preview", "evaluation", "learning", "deletion"}
_EVIDENCE = {
    "head": set(),
    "download": {"if_match"},
    "upload": {"sha256", "size_bytes", "content_type", "multipart"},
    "sign_download": {"expires_in", "download_name"},
    "sign_upload": {"expires_in"},
    "retire": set(),
    "discard_report": set(),
}
_CONDITIONAL_CONFLICT = {
    "PreconditionFailed",
    "412",
    "ConditionalRequestConflict",
    "409",
}
_CONTENT_TYPES = {
    "application/octet-stream",
    "application/zip",
    "application/json",
    "image/png",
}


def _denied():
    return SavedIOError("training_storage_identity_changed", status_code=409)


def _invalid(*, unsettled=False):
    return SavedIOError(
        "training_storage_invalid_receipt", status_code=409, unsettled=unsettled
    )


def _arguments(subject, action):
    try:
        if set(subject) != {
            "permit",
            "scope",
            "owner",
            "attempt",
            "member",
            "evidence",
        }:
            raise ValueError
        permit, owner = UUID(subject["permit"]), UUID(subject["owner"])
        attempt = UUID(subject["attempt"]) if subject["attempt"] is not None else None
        scope, member, evidence = (
            subject["scope"],
            subject["member"],
            subject["evidence"],
        )
        if scope not in _SCOPE or (
            member is not None and (not isinstance(member, str) or len(member) > 105)
        ):
            raise ValueError
        if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE[action]:
            raise ValueError
        if action == "upload" and (
            not re.fullmatch(r"[0-9a-f]{64}", evidence["sha256"])
            or type(evidence["size_bytes"]) is not int
            or not 0 <= evidence["size_bytes"] <= 24 * 1024**3
            or evidence["content_type"] not in _CONTENT_TYPES
            or type(evidence["multipart"]) is not bool
        ):
            raise ValueError
        if action.startswith("sign_") and (
            type(evidence["expires_in"]) is not int
            or not 0 < evidence["expires_in"] <= 600
        ):
            raise ValueError
        if (
            action == "sign_download"
            and evidence["download_name"] is not None
            and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", evidence["download_name"]
            )
        ):
            raise ValueError
        if (
            action == "download"
            and evidence["if_match"] is not None
            and (
                not isinstance(evidence["if_match"], str)
                or not 0 < len(evidence["if_match"]) <= 256
            )
        ):
            raise ValueError
    except (KeyError, AttributeError, TypeError, ValueError):
        raise _denied() from None
    return permit, scope, owner, attempt, member, action, evidence


async def _capture(context, args):
    fact = await context.fetch(_CAPTURE, *args)
    if not isinstance(fact, dict):
        raise _denied()
    return fact


def _position(fact):
    return (
        UUID(fact["location_id"]),
        fact["config_revision"],
        fact["connection_revision"],
        "training",
        fact["bucket"],
    )


async def _pin(context, fact):
    return await context.s3(*_position(fact), fact["key"], version=fact["version"])


async def _recheck(context, args, before):
    after = await _capture(context, args)
    if after != before:
        raise _denied()


def _same_receipt(response, expected):
    return response.get("ETag") == expected.get("ETag") and response.get(
        "VersionId"
    ) == expected.get("VersionId")


def _validate_metadata(fact, head):
    size = head.get("ContentLength")
    if (
        type(size) is not int
        or not 0 <= size <= 24 * 1024**3
        or (fact["size_bytes"] is not None and size != fact["size_bytes"])
        or not isinstance(head.get("ETag"), str)
        or not head["ETag"]
        or any(
            head.get("Metadata", {}).get(key) != value
            for key, value in fact.get("meta", {}).items()
        )
        or (fact["etag"] is not None and fact["etag"] != head["ETag"])
        or head.get("DeleteMarker")
        or head.get("ContentRange") is not None
    ):
        raise _invalid()
    return size


async def _exact(context, fact):
    pin = await _pin(context, fact)
    head = await pin.head(if_match=fact["etag"])
    _validate_metadata(fact, head)
    if fact["etag"] is None:
        pin = pin.with_version(head.get("VersionId"))
    return pin, head


def _receipt(fact, head):
    return {
        "sha256": fact["sha256"],
        "size_bytes": head["ContentLength"],
        "etag": head["ETag"],
        "version": head.get("VersionId"),
    }


async def _record(context, args, receipt):
    recorded = await context.fetch(_RECORD, *args, receipt)
    if not isinstance(recorded, dict) or (
        receipt is not None
        and (
            recorded["etag"] != receipt["etag"]
            or recorded["version"] != receipt["version"]
        )
    ):
        raise _denied()
    return recorded


async def _head(context, subject, _body=None):
    args = _arguments(subject, "head")
    fact = await _capture(context, args)
    _, head = await _exact(context, fact)
    await _recheck(context, args, fact)
    return head


class _VerifiedBody:
    def __init__(self, download, fact):
        self.download = download
        self.expected = fact["sha256"]
        self.digest = sha256() if self.expected is not None else None
        self.remaining = download.length

    def read(self, length):
        value = self.download.body.read(length)
        self.remaining -= len(value)
        if self.remaining < 0:
            raise _invalid()
        if self.digest is not None:
            self.digest.update(value)
        return value

    def verify(self):
        if self.remaining or (
            self.digest is not None and self.digest.hexdigest() != self.expected
        ):
            raise _invalid()

    def close(self):
        self.download.close()


async def _verified_download(pin, fact, head):
    opened = await pin.get(if_match=head["ETag"])
    if opened.length != head["ContentLength"] or not _same_receipt(
        opened.metadata, head
    ):
        opened.close()
        raise _invalid()
    return _VerifiedBody(opened, fact)


async def _download(context, subject, _body=None):
    args = _arguments(subject, "download")
    fact = await _capture(context, args)
    pin, head = await _exact(context, fact)
    if args[-1]["if_match"] not in (None, head["ETag"]):
        raise _denied()
    verified = await _verified_download(pin, fact, head)

    async def after_body():
        verified.verify()
        await _recheck(context, args, fact)
        if fact["etag"] is None and fact["sha256"] is not None:
            await _record(context, args, _receipt(fact, head))

    return Download(
        verified,
        length=head["ContentLength"],
        metadata=verified.download.metadata,
        after_body=after_body,
    )


async def _drain(context, pin, fact, head):
    verified = await _verified_download(pin, fact, head)
    try:
        while await context.run(verified.read, 1024 * 1024):
            pass
        verified.verify()
    finally:
        await context.run(verified.close)


async def _upload(context, subject, body):
    args = _arguments(subject, "upload")
    if fstat(body.fileno()).st_size != args[-1]["size_bytes"]:
        raise SavedIOError("upload_length_mismatch", status_code=400)
    fact = await _capture(context, args)
    if (await context.run(file_digest, body, "sha256")).hexdigest() != fact["sha256"]:
        raise _invalid()
    await context.run(body.seek, 0)
    await _recheck(context, args, fact)
    fact = await _record(context, args, None)
    pin = await _pin(context, fact)
    written = None
    if fact["etag"] is None:
        options = args[-1]
        metadata = dict(fact["meta"])
        if args[1] == "dataset" and args[4].startswith("media:"):
            metadata["sha256"] = fact["sha256"]
        try:
            written = await pin.put(
                body,
                length=fact["size_bytes"],
                content_type=options["content_type"],
                metadata=metadata,
                if_none_match="*",
                multipart=options["multipart"],
            )
        except SavedIOError as exc:
            if exc.unsettled or exc.storage_code not in _CONDITIONAL_CONFLICT:
                raise
    try:
        if written is not None:
            pin = pin.with_version(written.get("VersionId"))
            head = await pin.head(if_match=written["ETag"])
            _validate_metadata(fact, head)
            if not _same_receipt(head, written):
                raise _invalid(unsettled=True)
        else:
            pin, head = await _exact(context, fact)
        await _drain(context, pin, fact, head)
        await _recheck(context, args, fact)
        await _record(context, args, _receipt(fact, head))
    except Exception:
        if written is not None:
            raise _invalid(unsettled=True) from None
        raise
    current = False
    if written is not None and written.get("VersionId") not in (None, "null"):
        try:
            after = await pin.with_version(None).head()
            current = _same_receipt(after, head)
        except SavedIOError:
            pass
    return {"created": written is not None, "receipt": written, "current": current}


async def _sign_download(context, subject, _body=None):
    args = _arguments(subject, "sign_download")
    fact = await _capture(context, args)
    pin, head = await _exact(context, fact)
    if fact["etag"] is None:
        await _drain(context, pin, fact, head)
        fact = await _record(context, args, _receipt(fact, head))
    url = await pin.sign(
        "GET",
        expires_in=args[-1]["expires_in"],
        download_name=args[-1]["download_name"],
    )
    await _recheck(context, args, fact)
    return {"url": url}


async def _sign_upload(context, subject, _body=None):
    args = _arguments(subject, "sign_upload")
    fact = await _record(context, args, None)
    pin = await _pin(context, fact)
    metadata = {**fact["meta"], "sha256": fact["sha256"]}
    url = await pin.with_version(None).sign(
        "PUT",
        expires_in=args[-1]["expires_in"],
        length=fact["size_bytes"],
        metadata=metadata,
        if_none_match="*",
    )
    await _recheck(context, args, fact)
    return {
        "url": url,
        "headers": {
            "If-None-Match": "*",
            **{f"x-amz-meta-{name}": value for name, value in metadata.items()},
        },
    }


async def _settle_retirement(context, args, before):
    await _recheck(context, args, before)
    if await context.fetch(_RECORD, *args, {"retired": True}) != before:
        raise _denied()


async def _discard_report(context, subject, _body=None):
    args = _arguments(subject, "discard_report")
    fact = await _capture(context, args)
    pin = await _pin(context, fact)
    await pin.delete(if_match=fact["etag"] if fact["version"] is None else None)
    await _settle_retirement(context, args, fact)
    return {}


async def _retire(context, subject, _body=None):
    args = _arguments(subject, "retire")
    fact = await _capture(context, args)
    deleted = 0
    pin = await _pin(context, fact)
    async with (
        context.managed(*_position(fact), fact["key"]) as target,
        pin.client() as client,
    ):
        for _ in range(256):
            await _recheck(context, args, fact)
            page = await context.run(
                target.list_object_versions, Prefix=fact["key"], MaxKeys=1000
            )
            rows = page.get("Versions", []) + page.get("DeleteMarkers", [])
            if not rows and page.get("IsTruncated") is False:
                break
            if not rows or len(rows) > 1000:
                raise _invalid()
            objects = []
            for row in rows:
                if not row["Key"].startswith(fact["key"]) or not row.get("VersionId"):
                    raise _invalid()
                objects.append({"Key": row["Key"], "VersionId": row["VersionId"]})
            response = await context.run(
                client.delete_objects,
                Bucket=fact["bucket"],
                Delete={"Objects": objects, "Quiet": True},
            )
            if (
                response.get("Errors")
                or response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
            ):
                raise _invalid()
            deleted += len(objects)
        else:
            raise _invalid()
    await _settle_retirement(context, args, fact)
    return {"deleted": deleted}


def register(registry):
    registry.allow(_CAPTURE + "(uuid,text,uuid,uuid,text,text,jsonb)")
    registry.allow(_RECORD + "(uuid,text,uuid,uuid,text,text,jsonb,jsonb)")
    registry.add("training", "head", "command", _head, access="read")
    registry.add("training", "download", "download", _download, access="read")
    registry.add("training", "sign_download", "command", _sign_download, access="read")
    registry.add("training", "sign_upload", "command", _sign_upload, access="write")
    registry.add("training", "upload", "upload", _upload, access="write")
    registry.add(
        "training", "discard_report", "command", _discard_report, access="cleanup"
    )
    registry.add("training", "retire", "command", _retire, access="cleanup")
