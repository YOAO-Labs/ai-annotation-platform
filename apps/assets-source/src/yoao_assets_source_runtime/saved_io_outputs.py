"""Registered physical I/O for committed Identity, Diagnostics, Audit and Export owners."""

from datetime import datetime, timedelta, timezone
from functools import partial
from hashlib import sha256
import re
from uuid import UUID

from yoao_assets_source.saved_io import SavedIOError

CHUNK_BYTES = 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600
MISSING = frozenset({"404", "NoSuchKey", "NoSuchVersion", "NotFound"})
TOKEN = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"[0-9a-f]{64}")
MIME_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
ASSETS = "yoao_assets_v1."
EXPORT = "yoao_export_v1."
SUBJECTS = {
    "avatar": ("token",),
    "bug": ("upload_id",),
    "archive": ("archive_id",),
    "zip": ("output_id",),
    "member": ("bundle_id", "member_id"),
    "media": ("bundle_id",),
}
LIMITS = {
    "avatar": 2 * 1024**2,
    "bug": 10 * 1024**2,
    "archive": 64 * 1024**3,
    "zip": 64 * 1024**3,
    "member": 5 * 1024**3,
}


def _invalid():
    raise SavedIOError("invalid_request", status_code=400)


def _denied():
    raise SavedIOError("output_not_admitted", status_code=403)


def _changed():
    raise SavedIOError("output_integrity_mismatch")


def _uuid(value):
    try:
        result = UUID(value)
        if str(result) == value:
            return result
    except (AttributeError, TypeError, ValueError):
        pass
    _invalid()


def _parse(family, action, subject):
    fields = SUBJECTS[family]
    extra = (
        {"actor_id"} if family == "bug" and action == "write"
        else {"download_name"} if family == "zip" and action == "sign"
        else set()
    )
    if set(subject) != {"operation_id", *fields, *extra}:
        _invalid()
    operation = _uuid(subject["operation_id"])
    if family == "avatar":
        token = subject["token"]
        if not isinstance(token, str) or TOKEN.fullmatch(token) is None:
            _invalid()
        ids = (token,)
    else:
        ids = tuple(_uuid(subject[field]) for field in fields)
    return operation, ids


async def _subject(context, family, action, operation, ids, subject):
    if family == "avatar":
        facts = await context.fetch(ASSETS + "saved_avatar_subject", *ids, operation, action)
    elif family == "bug":
        actor = _uuid(subject["actor_id"]) if action == "write" else None
        facts = await context.fetch(ASSETS + "saved_bug_subject", *ids, operation, action, actor)
    elif family == "archive":
        facts = await context.fetch(ASSETS + "saved_archive_subject", *ids, operation, action)
    else:
        capability = {
            "zip": "zip_io_subject",
            "member": "media_member_io_subject",
            "media": "media_prefix_io_subject",
        }[family]
        facts = await context.fetch(EXPORT + capability, *ids, operation, action)
    if not isinstance(facts, dict):
        _denied()
    return facts


def _suffix(family, ids, facts):
    if family == "avatar":
        return "avatars", f"{ids[0]}.webp"
    if family == "bug":
        extension = MIME_EXTENSIONS.get(facts["mime_type"])
        if extension is None:
            _denied()
        return "bug-reports", f"bug-report-attachments/{facts['owner_id']}/{ids[0]}.{extension}"
    if family == "archive":
        return "audit-archive", f"{facts['year']:04d}/{facts['month']:02d}/{ids[0]}.jsonl.gz"
    if family in {"member", "media"}:
        return "export", f"lidar-export-media/objects/{ids[0]}/"
    suffix = f"/{facts['project_id']}/{facts['job_id']}/{ids[0]}.zip"
    key = facts["storage_ref"]["object_key"]
    for media in ("image", "video"):
        if key.endswith(media + suffix):
            return "export", media + suffix
    _denied()


async def _origin(context, family, ids, facts):
    purpose, suffix = _suffix(family, ids, facts)
    origin = await context.fetch(ASSETS + "saved_output_origin", facts["storage_ref"], purpose, suffix)
    if not isinstance(origin, dict):
        _denied()
    return (
        _uuid(origin["location_id"]), origin["config_revision"],
        origin["connection_revision"], purpose, origin["bucket"],
    )


async def _pin(context, family, ids, facts):
    origin = await _origin(context, family, ids, facts)
    ref = facts["storage_ref"]
    if family == "member":
        key = f"{ref['key_prefix']}{ids[1]}.bin"
        if facts.get("object_key") != key:
            _denied()
    else:
        key = ref["object_key"]
    length, digest = facts.get("size_bytes"), facts.get("sha256")
    if (type(length) is not int or not 0 <= length <= LIMITS[family]
            or not isinstance(digest, str) or DIGEST.fullmatch(digest) is None):
        _denied()
    return await context.s3(*origin, key, version=facts.get("version_id"))


def _plain(value, limit=1024):
    return isinstance(value, str) and 0 < len(value) <= limit and all(
        ord(char) >= 32 and ord(char) != 127 for char in value
    )


def _same_version(expected, actual):
    return expected == actual or expected == "null" and actual is None


def _check_observation(response, facts, *, etag, version):
    if (response.get("ContentLength") != facts["size_bytes"] or response.get("ETag") != etag
            or not _same_version(version, response.get("VersionId"))
            or response.get("ContentRange") is not None or response.get("DeleteMarker")):
        _changed()


def _hash(stream, size):
    digest = sha256()
    remaining = size
    while remaining:
        chunk = stream.read(min(CHUNK_BYTES, remaining))
        if not chunk or len(chunk) > remaining:
            _changed()
        digest.update(chunk)
        remaining -= len(chunk)
    if stream.read(1):
        _changed()
    return digest.hexdigest()


async def _require_unversioned(pin):
    if (await pin.get_bucket_versioning()).get("Status") is not None:
        _changed()


async def _open(context, family, pin, facts):
    if pin.version is None and family in {"member", "zip"}:
        await _require_unversioned(pin)
    head = await pin.head()
    etag = head.get("ETag")
    version = pin.version if pin.version is not None else head.get("VersionId")
    if not _plain(etag) or (facts.get("etag") is not None and facts["etag"] != etag):
        _changed()
    _check_observation(head, facts, etag=etag, version=version)
    result = await pin.get(if_match=etag)
    try:
        _check_observation(result.metadata, facts, etag=etag, version=version)
    except BaseException:
        await context.run(result.close)
        raise
    return result, etag, version


async def _verify(context, family, pin, facts):
    result, etag, version = await _open(context, family, pin, facts)
    try:
        if await context.run(_hash, result.body, facts["size_bytes"]) != facts["sha256"]:
            _changed()
    finally:
        await context.run(result.close)
    after = await pin.head(if_match=etag)
    _check_observation(after, facts, etag=etag, version=version)


async def _write(context, family, operation, ids, subject, facts, pin, body):
    length = facts["size_bytes"]
    if pin.version is not None or await context.run(_hash, body, length) != facts["sha256"]:
        _changed()
    await context.run(body.seek, 0)
    if await _subject(context, family, "write", operation, ids, subject) != facts:
        _denied()
    identifier = ":".join(map(str, ids))
    evidence = {name: facts[name] for name in ("storage_ref", "sha256", "size_bytes")}
    if not await context.fetch(ASSETS + "begin_saved_output_write", operation, family, identifier, evidence):
        raise SavedIOError("output_write_already_started")
    content_type = {
        "avatar": "image/webp", "bug": facts.get("mime_type"), "archive": "application/gzip",
        "zip": "application/zip", "member": "application/octet-stream",
    }[family]
    cache_control = {
        "avatar": "public, max-age=86400, stale-while-revalidate=604800",
        "member": "private, max-age=604800, immutable",
    }.get(family)
    written = await pin.put(
        body, length=length, if_none_match="*", content_type=content_type,
        content_fields={"CacheControl": cache_control} if cache_control is not None else None,
        multipart=family == "zip" and length >= 8 * 1024**2 or length > 5 * 1024**3,
    )
    etag, version = written.get("ETag"), written.get("VersionId")
    if not _plain(etag) or (version is not None and not _plain(version)):
        raise SavedIOError("output_invalid_write_ack", status_code=503, unsettled=True)
    if family in {"zip", "member"} and version == "null":
        version = None
    if version is None:
        await _require_unversioned(pin)
    receipt = {"etag": etag, "version_id": version}
    await _verify(context, family, pin.with_version(version), facts | receipt)
    if await _subject(context, family, "write", operation, ids, subject) != facts:
        _denied()
    if family == "zip":
        if not await context.fetch(EXPORT + "save_source_zip_proof", *ids, operation, receipt):
            _denied()
    elif family == "member" and version is not None:
        if not await context.fetch(EXPORT + "save_source_media_version", *ids, operation, version):
            _denied()
    if not await context.fetch(ASSETS + "finish_saved_output_write", operation, family, identifier, receipt):
        raise SavedIOError("output_receipt_unsettled", status_code=503, unsettled=True)
    return receipt


async def _download(context, family, operation, ids, subject, facts, pin):
    result, etag, version = await _open(context, family, pin, facts)

    async def check():
        _check_observation(await pin.head(if_match=etag), facts, etag=etag, version=version)
        if await _subject(context, family, "read", operation, ids, subject) != facts:
            _denied()

    result.after_body = check
    return result


def _missing(error):
    return error.status_code == 404 or error.storage_code in MISSING


async def _delete(context, family, operation, ids, subject, facts, pin):
    etag = None
    if pin.version is None:
        await _require_unversioned(pin)
    if pin.version in {None, "null"}:
        try:
            result, etag, version = await _open(context, family, pin, facts)
        except SavedIOError as exc:
            if _missing(exc):
                return {"absent": True}
            raise
        try:
            if version not in {None, "null"} or await context.run(_hash, result.body, facts["size_bytes"]) != facts["sha256"]:
                _changed()
        finally:
            await context.run(result.close)
    else:
        try:
            await pin.head()
        except SavedIOError as exc:
            if _missing(exc):
                return {"absent": True}
            raise
    if await _subject(context, family, "delete", operation, ids, subject) != facts:
        _denied()
    response = await pin.delete(if_match=etag)
    if response.get("DeleteMarker") or (pin.version is not None and not _same_version(pin.version, response.get("VersionId"))):
        _changed()
    try:
        await pin.head()
    except SavedIOError as exc:
        if _missing(exc):
            return {"absent": True}
        raise
    _changed()


async def _sign(family, facts, pin, subject):
    current = datetime.now(timezone.utc)
    deadline = datetime.fromisoformat(facts["expires_at"])
    fixture = deadline - datetime.fromisoformat(facts["created_at"]) == timedelta(minutes=15)
    seconds = min(RETENTION_SECONDS, int((deadline - current).total_seconds()) - int(fixture))
    if seconds <= 0:
        raise SavedIOError("output_expired", status_code=410)
    name = subject.get("download_name")
    if family == "zip" and (not _plain(name, 255) or any(char in name for char in '\\"')):
        _invalid()
    if pin.version is None:
        await _require_unversioned(pin)
    url = await pin.sign("GET", expires_in=seconds, download_name=name)
    if fixture and datetime.now(timezone.utc) + timedelta(seconds=seconds) > deadline:
        raise SavedIOError("output_expired", status_code=410)
    return {"url": url}


def _inventory(target, expected, *, match_size=True):
    visible, tokens, token = {}, set(), None
    while True:
        result = target.list_objects_v2(MaxKeys=1000, ContinuationToken=token)
        for row in result.get("Contents", ()):
            key, size = row.get("Key"), row.get("Size")
            if (
                key not in expected or key in visible or type(size) is not int or size < 0
                or match_size and size != expected[key]["size_bytes"]
            ):
                _changed()
            visible[key] = size
        truncated = result.get("IsTruncated")
        if truncated is False:
            return visible
        token = result.get("NextContinuationToken")
        if truncated is not True or not _plain(token, 4096) or token in tokens:
            _changed()
        tokens.add(token)


def _owned_versions(target, expected, births):
    visible = _inventory(target, expected, match_size=False)
    markers, pages, versions, keys, selected = {}, set(), set(), set(), []
    unversioned = None
    while True:
        response = target.list_object_versions(MaxKeys=1000, **markers)
        for field in ("Versions", "DeleteMarkers"):
            for row in response.get(field, ()):
                key, version = row.get("Key"), row.get("VersionId")
                if key not in expected or not _plain(version) or (key, version) in versions:
                    _changed()
                keys.add(key)
                versions.add((key, version))
                if len(versions) > len(expected) * 2:
                    _changed()
                birth = births.get(key)
                if birth is not None:
                    if version == birth:
                        if field != "Versions":
                            _changed()
                        selected.append((key, version))
                else:
                    if version != "null" or field != "Versions":
                        _changed()
                    if unversioned is None:
                        unversioned = target.get_bucket_versioning().get("Status") is None
                    if not unversioned:
                        _changed()
                    selected.append((key, version))
        truncated = response.get("IsTruncated")
        if truncated is False:
            if not visible.keys() <= keys:
                _changed()
            return selected
        key = response.get("NextKeyMarker")
        version = response.get("NextVersionIdMarker")
        if truncated is not True or key not in expected or (key, version) in pages:
            _changed()
        pages.add((key, version))
        markers = {"KeyMarker": key, "VersionIdMarker": version or None}


async def _media(context, action, operation, ids, subject, facts):
    origin = await _origin(context, "media", ids, facts)
    prefix = facts["storage_ref"]["key_prefix"]
    expected = facts.get("inventory", {}).get("objects")
    if (facts.get("key_prefix") != prefix or not isinstance(expected, dict) or len(expected) > 21_000):
        _denied()
    async with context.managed(*origin, prefix) as target:
        if action == "list":
            if len(await context.run(_inventory, target, expected)) != len(expected):
                _changed()
            if await _subject(context, "media", action, operation, ids, subject) != facts:
                _denied()
            return {"verified": True}
        births = facts["member_versions"]
        owned = await context.run(_owned_versions, target, expected, births)
        if await _subject(context, "media", action, operation, ids, subject) != facts:
            _denied()
        for key, version in owned:
            pin = target.object(key, version=version)
            if version == "null":
                await _require_unversioned(pin)
                await _verify(context, "member", pin, expected[key])
            response = await pin.delete()
            if response.get("DeleteMarker") or not _same_version(version, response.get("VersionId")):
                _changed()
        if await context.run(_owned_versions, target, expected, births):
            _changed()
        if await _subject(context, "media", action, operation, ids, subject) != facts:
            _denied()
        return {"removed": len(owned)}


async def _handle(family, action, context, subject, body=None):
    operation, ids = _parse(family, action, subject)
    facts = await _subject(context, family, action, operation, ids, subject)
    if family == "media":
        return await _media(context, action, operation, ids, subject, facts)
    pin = await _pin(context, family, ids, facts)
    if action == "write":
        return await _write(context, family, operation, ids, subject, facts, pin, body)
    if action == "read":
        return await _download(context, family, operation, ids, subject, facts, pin)
    if action == "verify":
        await _verify(context, family, pin, facts)
        if await _subject(context, family, action, operation, ids, subject) != facts:
            _denied()
        return {"verified": True}
    if action == "sign":
        reply = await _sign(family, facts, pin, subject)
        if await _subject(context, family, action, operation, ids, subject) != facts:
            _denied()
        return reply
    if action == "delete":
        reply = await _delete(context, family, operation, ids, subject, facts, pin)
        if await _subject(context, family, action, operation, ids, subject) != facts:
            _denied()
        return reply
    _invalid()


async def _write_status(context, subject, body=None):
    family = subject.get("kind")
    if not isinstance(family, str) or family not in LIMITS:
        _invalid()
    operation, ids = _parse(family, "status", {key: value for key, value in subject.items() if key != "kind"})
    result = await context.fetch(ASSETS + "saved_output_write_status", operation, family, ":".join(map(str, ids)))
    if not isinstance(result, dict):
        raise SavedIOError("output_receipt_unsettled", status_code=503, unsettled=True)
    return result


def register(registry):
    for purpose, signatures in (
        (ASSETS, (
            "saved_output_origin(jsonb,text,text)", "saved_avatar_subject(text,uuid,text)",
            "saved_bug_subject(uuid,uuid,text,uuid)", "saved_archive_subject(uuid,uuid,text)",
            "begin_saved_output_write(uuid,text,text,jsonb)", "finish_saved_output_write(uuid,text,text,jsonb)",
            "saved_output_write_status(uuid,text,text)",
        )),
        (EXPORT, (
            "zip_io_subject(uuid,uuid,text)", "media_member_io_subject(uuid,uuid,uuid,text)",
            "media_prefix_io_subject(uuid,uuid,text)", "save_source_zip_proof(uuid,uuid,jsonb)",
            "save_source_media_version(uuid,uuid,uuid,text)",
        )),
    ):
        for signature in signatures:
            registry.allow(purpose + signature)
    for family, actions in (
        ("avatar", ("write", "read", "delete")),
        ("bug", ("write", "read", "delete")),
        ("archive", ("write", "read")),
        ("zip", ("write", "verify", "sign", "delete")),
        ("member", ("write", "read", "verify", "sign")),
        ("media", ("list", "list_versions")),
    ):
        for action in actions:
            mode, access = {
                "write": ("upload", "write"), "read": ("download", "read"),
                "delete": ("command", "cleanup"), "list_versions": ("command", "cleanup"),
            }.get(action, ("command", "read"))
            name = "delete" if action == "list_versions" else action
            registry.add("outputs", f"{family}_{name}", mode, partial(_handle, family, action), access=access)
    registry.add("outputs", "write_status", "command", _write_status, access="write")
