"""Finite physical operations for committed migration decisions and sealed routes."""

import asyncio
from functools import partial
import hashlib
import json
import re
import sqlite3
from uuid import UUID

from yoao_assets_source.saved_io import SavedIOError

from .migration_flights import MigrationFlights

_COPY = "yoao_assets_v1.migration_io_copy"
_BASELINE = "yoao_assets_v1.migration_io_baseline"
_CLAIM = "yoao_assets_v1.migration_io_claim_write"
_READ = "yoao_assets_v1.migration_io_read"
_CUTOVER = "yoao_assets_v1.migration_io_cutover"
_COPY_SIGNATURE = "uuid,uuid,text,uuid,text,integer,integer,text"
_COPY_FIELDS = {
    "copy_id",
    "attempt_id",
    "task_id",
    "operation_id",
    "phase",
    "location_revision",
    "connection_revision",
}
_READ_FIELDS = {
    "publication_id",
    "publication_revision",
    "object_id",
    "version",
    "operation_id",
    "location_revision",
    "connection_revision",
}
_CUTOVER_FIELDS = {
    "publication_id",
    "publication_revision",
    "finalization_revision",
    "window_revision",
    "actor_id",
    "purpose",
    "generation",
    "operation_id",
    "phase",
    "location_revision",
    "connection_revision",
}
_PURPOSES = {
    "annotations",
    "audit-archive",
    "avatars",
    "bug-reports",
    "datasets",
    "export",
    "import",
    "media-cache",
    "training",
}
_CONTENT = (
    "ContentType",
    "CacheControl",
    "ContentDisposition",
    "ContentEncoding",
    "ContentLanguage",
    "Expires",
    "Metadata",
)
_RESERVED = {"aap-copy-plan", "aap-copy-object", "aap-copy-nonce"}
_MUTABLE = {"state", "nonce", "receipt", "error", "content"}
_BASELINE_LIMIT = 256 * 1024**2
_HEX = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[0-9a-f]{48}")
_RANGE = re.compile(r"bytes=([0-9]{1,19})-([0-9]{1,19})")
_CHUNK = 1024**2


def _deny(code="migration_io_unproven"):
    raise SavedIOError(code, status_code=409)


def _text(value, limit):
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= limit
        or any(ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        _deny()
    return value


def _uuid(value):
    return UUID(_text(value, 36))


def _integer(value, upper=2147483647):
    if type(value) is not int or not 1 <= value <= upper:
        _deny()
    return value


def _hex(value, pattern=_HEX):
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        _deny()
    return value


def _fields(subject, required):
    if not isinstance(subject, dict) or subject.keys() != required:
        _deny()


def _canonical(value):
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        + b"\n"
    )


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _decoded(value):
    if value is None or not isinstance(value[0], str) or len(value[0]) > 262144:
        _deny("copy_checkpoint_integrity")
    data = json.loads(value[0])
    if not isinstance(data, dict) or _digest(data) != value[1]:
        _deny("copy_checkpoint_integrity")
    return data


def _open_baseline(blob, digest):
    if (
        not isinstance(blob, bytes)
        or not 0 < len(blob) <= _BASELINE_LIMIT
        or hashlib.sha256(blob).hexdigest() != digest
    ):
        _deny("copy_checkpoint_integrity")
    db = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        db.deserialize(blob)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA cache_size=-2048")
        meta = _decoded(
            db.execute("SELECT data, digest FROM main.metadata WHERE id=1").fetchone()
        )
        if meta.get("schema") not in (
            "aap.storage-migration-copy/v1",
            "aap.storage-migration-copy/v2",
        ):
            _deny("copy_checkpoint_integrity")
        return db, meta
    except BaseException:
        db.close()
        raise


class _Baselines:
    """At most one verified immutable SQLite checkpoint; deltas always come from PG."""

    def __init__(self):
        self.lock = asyncio.Lock()
        self.cached = None

    async def read(self, context, args, proof, object_id):
        digest = _hex(proof["baseline_sha256"])
        async with self.lock:
            try:
                if self.cached is None or self.cached[0] != digest:
                    if self.cached is not None:
                        _, old, _ = self.cached
                        self.cached = None
                        await context.run(old.close)
                    blob = await context.fetch(_BASELINE, *args, digest)
                    db, meta = await context.run(_open_baseline, blob, digest)
                    self.cached = digest, db, meta
                _, db, meta = self.cached
                row = await context.run(_baseline_row, db, object_id)
                return meta, row
            except (KeyError, TypeError, ValueError, sqlite3.Error):
                _deny("copy_checkpoint_integrity")


def _baseline_row(db, object_id):
    return _decoded(
        db.execute(
            "SELECT data, digest FROM main.results WHERE id=?", (object_id,)
        ).fetchone()
    )


def _copy_args(subject):
    phase = subject["phase"]
    if phase not in ("run", "verify"):
        _deny()
    return (
        _uuid(subject["copy_id"]),
        _uuid(subject["attempt_id"]),
        _text(subject["task_id"], 64),
        _uuid(subject["operation_id"]),
        phase,
        _integer(subject["location_revision"]),
        _integer(subject["connection_revision"]),
    )


async def _proof(context, args, object_id):
    proof = await context.fetch(_COPY, *args, object_id)
    if not isinstance(proof, dict) or proof["allowlist"] != sorted(
        await context.connector_allowlist()
    ):
        _deny()
    return proof


def _copy_row(proof, meta, original, object_id, obligation):
    row = proof["delta"]
    target = proof["target"]
    if row is None:
        row = original
    elif (
        not isinstance(row, dict)
        or _digest(row) != proof["delta_digest"]
        or {k: v for k, v in row.items() if k not in _MUTABLE}
        != {k: v for k, v in original.items() if k not in _MUTABLE}
        or (original.get("nonce") and row.get("nonce") != original["nonce"])
        or (
            original.get("receipt")
            and (row.get("receipt") or {}).get("version_id")
            != original["receipt"]["version_id"]
        )
    ):
        _deny("copy_checkpoint_integrity")
    source = proof["source"]
    frozen = meta["target"]
    source_address = row["source"]
    creator = row.get("creation_plan_uuid", meta["plan_uuid"])
    key = f"{target['key_prefix']}{creator}/{object_id}/{_hex(row['sha256'])}"
    limits = proof["limits"]
    if (
        row["decision"] != "copy"
        or row["source_kind"] != "object"
        or row["id"] != object_id
        or _digest(source_address) != object_id
        or source_address["endpoint_url"] != source["endpoint"]
        or source_address["bucket"] not in source["purpose_buckets"].values()
        or creator not in [meta["plan_uuid"], *meta.get("ancestor_plan_uuids", [])]
        or str(UUID(creator)) != creator
        or frozen["endpoint_url"] != target["endpoint_url"]
        or frozen["bucket"] != target["bucket"]
        or frozen["key_prefix"] != target["key_prefix"]
        or meta.get("generation", 1) != proof["generation"]
        or row["target_key"] != key
        or len(key.encode()) > 1024
        or hashlib.sha256(key.encode()).hexdigest() != obligation
        or type(row["size_bytes"]) is not int
        or not 0 <= row["size_bytes"] <= min(limits["max_object_bytes"], 1024**3)
    ):
        _deny()
    return row, creator, key


async def _pin(context, target, key, *, version=None, purpose="migration"):
    return await context.s3(
        UUID(target["location_id"]),
        target["location_revision"],
        target["connection_revision"],
        purpose,
        target["bucket"],
        key,
        version=version,
    )


def _creation(row, creator):
    fields = row.get("content")
    if not isinstance(fields, dict) or fields.keys() - set(_CONTENT):
        _deny("copy_checkpoint_integrity")
    metadata = fields.get("Metadata", {})
    if (
        not isinstance(metadata, dict)
        or any(
            not isinstance(k, str) or not isinstance(v, str) or k.lower() in _RESERVED
            for k, v in metadata.items()
        )
        or any(not isinstance(v, str) for k, v in fields.items() if k != "Metadata")
    ):
        _deny("copy_checkpoint_integrity")
    return {
        **fields,
        "Metadata": {
            **metadata,
            "aap-copy-plan": creator,
            "aap-copy-object": row["id"],
            "aap-copy-nonce": _hex(row.get("nonce"), _NONCE),
        },
    }


def _content(metadata):
    return {
        **{k: metadata[k] for k in _CONTENT if k in metadata},
        "Metadata": metadata.get("Metadata", {}),
    }


def _validate(
    metadata, expected_version, size, *, etag=None, status=200, content_range=None
):
    actual = metadata.get("VersionId")
    if (
        not isinstance(actual, str)
        or actual in ("", "null")
        or (expected_version is not None and actual != expected_version)
        or (etag is not None and metadata.get("ETag") != etag)
        or not metadata.get("ETag")
        or metadata.get("DeleteMarker")
        or metadata.get("ContentLength") != size
        or metadata.get("ContentRange") != content_range
        or metadata.get("ResponseMetadata", {}).get("HTTPStatusCode") != status
    ):
        _deny("migration_physical_identity_changed")
    return actual


def _body_matches(body, size, digest):
    sha = hashlib.sha256()
    total = 0
    body.seek(0)
    while part := body.read(min(_CHUNK, size - total + 1)):
        total += len(part)
        if total > size:
            return False
        sha.update(part)
    body.seek(0)
    return total == size and sha.hexdigest() == digest


async def _history(pin, key):
    listed = await pin.list_object_versions(max_keys=2)
    return listed.get("IsTruncated") is True or any(
        version.get("Key") == key
        for category in ("Versions", "DeleteMarkers")
        for version in listed.get(category, ())
    )


async def _copy_versioning(flights, context, subject, body=None):
    _fields(subject, _COPY_FIELDS)
    try:
        args = _copy_args(subject)
        proof = await _proof(context, args, None)
        target = proof["target"]
        pin = await _pin(context, target, target["key_prefix"] + ".copy-versioning")
        return await flights.run(context, args[3], "copy", pin.get_bucket_versioning)
    except (KeyError, ValueError, TypeError):
        _deny()


def _copy_objects(cache, flights, action):
    extra = {
        "copy_history": set(),
        "copy_head": {"version"},
        "copy_get": {"version"},
        "copy_write": {"nonce"},
    }[action]

    async def handle(context, subject, body=None):
        _fields(subject, _COPY_FIELDS | {"object_id", "obligation"} | extra)
        try:
            return await _copy_object(context, subject, body, action, cache, flights)
        except (KeyError, ValueError, TypeError, AttributeError):
            _deny()

    return handle


async def _copy_object(context, subject, body, action, cache, flights):
    args = _copy_args(subject)
    object_id, obligation = _hex(subject["object_id"]), _hex(subject["obligation"])
    proof = await _proof(context, args, object_id)
    meta, original = await cache.read(context, args, proof, object_id)
    row, creator, key = _copy_row(proof, meta, original, object_id, obligation)
    return await flights.run(
        context,
        args[3],
        "copy",
        partial(
            _copy_physical,
            context,
            subject,
            body,
            action,
            args,
            proof,
            row,
            creator,
            key,
        ),
    )


async def _copy_physical(
    context, subject, body, action, args, proof, row, creator, key
):
    target = proof["target"]
    receipt = row.get("receipt")
    if args[4] == "verify" and (not receipt or action not in ("copy_head", "copy_get")):
        _deny()
    if action == "copy_history":
        if receipt or (row["state"] == "failed" and not proof["retry_failed"]):
            _deny()
        pin = await _pin(context, target, key)
        return {"IsTruncated": await _history(pin, key)}
    expected = _creation(row, creator)
    if action == "copy_write":
        if proof["write_claimed"]:
            _deny("copy_write_replayed")
        if (
            args[4] != "run"
            or row["state"] != "writing"
            or receipt
            or _hex(subject["nonce"], _NONCE) != row["nonce"]
            or not await context.run(
                _body_matches, body, row["size_bytes"], row["sha256"]
            )
        ):
            _deny("copy_write_unproven")
        pin = await _pin(context, target, key)
        if (await pin.get_bucket_versioning()).get("Status") != "Enabled":
            _deny("target_versioning_required")
        if await _history(pin, key):
            _deny("target_history_conflict")
        claimed = await context.fetch(
            _CLAIM, *args, row["id"], proof["delta_sequence"], row["nonce"]
        )
        if not claimed or claimed.get("claimed") is not True:
            _deny("copy_write_replayed")
        return await pin.put(
            body,
            length=row["size_bytes"],
            if_none_match="*",
            metadata=expected["Metadata"],
            content_fields={k: v for k, v in expected.items() if k != "Metadata"},
        )

    version = subject["version"]
    if version is not None:
        _text(version, 1024)
    size = row["size_bytes"]
    if receipt is not None:
        if version != receipt["version_id"]:
            _deny()
        pin = await _pin(context, target, key, version=version)
        etag = receipt.get("etag")
    else:
        pin = await _pin(context, target, key, version=version)
        current = await pin.head()
        _validate(current, version, size)
        if _content(current) != expected:
            _deny("target_creation_conflict")
        if action == "copy_head":
            return current
        if version is None:
            _deny()
        etag = current.get("ETag")

    if action == "copy_head":
        response = await pin.head(if_match=etag)
        _validate(response, version, size, etag=etag)
        if _content(response) != expected:
            _deny("target_creation_conflict")
        return response
    download = await pin.get(if_match=etag)
    try:
        _validate(download.metadata, version, size, etag=etag)
        if _content(download.metadata) != expected:
            _deny("target_response_mismatch")
    except BaseException:
        await context.run(download.close)
        raise
    return download


def _range(raw, size):
    if raw is None:
        return None, size, 200, None
    match = _RANGE.fullmatch(_text(raw, 45))
    if match is None:
        _deny()
    start, end = map(int, match.groups())
    if start > end or end >= size:
        _deny()
    return raw, end - start + 1, 206, f"bytes {start}-{end}/{size}"


def _retained_metadata(
    metadata, proof, object_id, version, size, status=200, content_range=None
):
    _validate(
        metadata,
        version,
        size,
        etag=proof["etag"],
        status=status,
        content_range=content_range,
    )
    content = _content(metadata)
    fields = content["Metadata"]
    if (
        fields.get("aap-copy-plan") != proof["plan"]
        or fields.get("aap-copy-object") != object_id
        or fields.get("aap-copy-nonce") != proof["nonce"]
        or _digest(content) != proof["content_sha256"]
    ):
        _deny("read_publication_changed")


def _retained(flights, action):
    extra = {
        "retained_head": set(),
        "retained_get": {"range", "condition"},
        "retained_sign": {"expires_in", "disposition", "content_type"},
    }[action]

    async def handle(context, subject, body=None):
        _fields(subject, _READ_FIELDS | extra)
        try:
            return await _read_route(context, subject, action, flights)
        except (KeyError, ValueError, TypeError, AttributeError):
            _deny("read_publication_changed")

    return handle


async def _read_route(context, subject, action, flights):
    object_id = _hex(subject["object_id"])
    version = _text(subject["version"], 1024)
    sign = action == "retained_sign"
    if version == "null":
        _deny()
    args = (
        _uuid(subject["operation_id"]),
        _uuid(subject["publication_id"]),
        _integer(subject["publication_revision"]),
        object_id,
        version,
        _integer(subject["location_revision"]),
        _integer(subject["connection_revision"]),
        sign,
    )
    proof = await context.fetch(_READ, *args)
    if not isinstance(proof, dict) or not proof["key"].startswith(
        proof["target"]["key_prefix"]
    ):
        _deny("read_publication_changed")
    return await flights.run(
        context,
        args[0],
        "read",
        partial(_read_physical, context, subject, action, args, proof),
    )


async def _read_physical(context, subject, action, args, proof):
    _, _, _, object_id, version, _, _, sign = args

    async def recheck():
        current = await context.fetch(_READ, *args)
        if not isinstance(current, dict) or any(
            current.get(field) != proof[field]
            for field in (
                "target",
                "key",
                "etag",
                "size",
                "content_sha256",
                "nonce",
                "plan",
            )
        ):
            _deny("read_publication_changed")
        return current

    pin = await _pin(context, proof["target"], proof["key"], version=version)
    if action == "retained_head" or sign:
        response = await pin.head(if_match=proof["etag"])
        _retained_metadata(response, proof, object_id, version, proof["size"])
        current = await recheck()
        if not sign:
            return response
        expiry = min(
            _integer(subject["expires_in"], 7 * 24 * 3600), current["sign_seconds"]
        )
        url = await pin.sign(
            "GET",
            expires_in=expiry,
            response_content_disposition=None
            if subject["disposition"] is None
            else _text(subject["disposition"], 1024),
            response_content_type=None
            if subject["content_type"] is None
            else _text(subject["content_type"], 255),
        )
        await recheck()
        return {"url": url}

    interval, size, status, content_range = _range(subject["range"], proof["size"])
    condition = subject["condition"]
    if condition not in ("match", "probe") or (
        condition == "probe" and interval is not None
    ):
        _deny()
    etag = proof["etag"]
    if condition == "probe":
        etag = (
            '"aap-impossible-etag-2"'
            if etag == '"aap-impossible-etag"'
            else '"aap-impossible-etag"'
        )
    download = await pin.get(range=interval, if_match=etag)
    try:
        if condition == "probe":
            _deny("content_conditions_unsupported")
        _retained_metadata(
            download.metadata, proof, object_id, version, size, status, content_range
        )
    except BaseException:
        await context.run(download.close)
        raise
    download.after_body = recheck
    return download


async def _cutover(flights, context, subject, body=None):
    _fields(subject, _CUTOVER_FIELDS)
    try:
        purpose = subject["purpose"]
        if purpose not in _PURPOSES or subject["phase"] != "preview":
            _deny()
        operation_id = _uuid(subject["operation_id"])
        target = await context.fetch(
            _CUTOVER,
            _uuid(subject["publication_id"]),
            _integer(subject["publication_revision"]),
            _integer(subject["finalization_revision"]),
            _integer(subject["window_revision"]),
            _uuid(subject["actor_id"]),
            purpose,
            _integer(subject["generation"]),
            operation_id,
            _integer(subject["location_revision"]),
            _integer(subject["connection_revision"]),
        )
        if not isinstance(target, dict):
            _deny("cutover_targets_changed")
        prefix = target["base_prefix"] + (
            "_yoao_managed/export/"
            if purpose == "export"
            else f"_aap_managed/{purpose}/"
        )
        return await flights.run(
            context,
            operation_id,
            "cutover",
            partial(
                context.probe,
                UUID(target["location_id"]),
                target["location_revision"],
                target["connection_revision"],
                purpose,
                target["bucket"],
                prefix,
            ),
        )
    except (KeyError, ValueError, TypeError):
        _deny("cutover_targets_changed")


def register(registry):
    for signature in (
        f"{_COPY}({_COPY_SIGNATURE})",
        f"{_BASELINE}({_COPY_SIGNATURE})",
        f"{_CLAIM}({_COPY_SIGNATURE},integer,text)",
        f"{_READ}(uuid,uuid,integer,text,text,integer,integer,boolean)",
        f"{_CUTOVER}(uuid,integer,integer,integer,uuid,text,integer,uuid,integer,integer)",
    ):
        registry.allow(signature)
    cache, flights = _Baselines(), MigrationFlights(registry)
    registry.add(
        "migration",
        "copy_versioning",
        "command",
        partial(_copy_versioning, flights),
        access="write",
    )
    for action, mode in (
        ("copy_history", "command"),
        ("copy_head", "command"),
        ("copy_get", "download"),
        ("copy_write", "upload"),
    ):
        registry.add(
            "migration",
            action,
            mode,
            _copy_objects(cache, flights, action),
            access="write",
        )
    for action, mode in (
        ("retained_head", "command"),
        ("retained_get", "download"),
        ("retained_sign", "command"),
    ):
        registry.add(
            "migration", action, mode, _retained(flights, action), access="read"
        )
    registry.add(
        "migration",
        "probe_cutover_target",
        "command",
        partial(_cutover, flights),
        access="cutover",
    )
