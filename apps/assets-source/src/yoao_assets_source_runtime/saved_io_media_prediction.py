"""The existing prediction lease's immutable input delivery and exact retirement."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import re

from botocore.exceptions import ClientError
from yoao_assets_source.saved_io import SavedIOError

from .saved_io_media import (
    _FAMILY,
    _denied,
    _digest,
    _fields,
    _identity,
    _mismatch,
    _pin,
    _receipt,
    _uuid,
)
from .saved_io_storage import physical_error

_SQL = "yoao_assets_v1.media_io_prediction_input"
_RELATIVE = re.compile(
    r"(?:video/[a-f0-9]{64}\.(?:mov|mkv|avi|asf|flv)|original/[a-f0-9]{64}\.(?:jpg|png|webp)|crops/(?:0|[1-9][0-9]{0,9})-[a-f0-9]{64}\.jpg)"
)
_MISSING = {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}


def _member(entry, relative):
    if (
        type(entry) is not dict
        or type(relative) is not str
        or not _RELATIVE.fullmatch(relative)
    ):
        raise _denied()
    size, digest = _receipt(
        {"size": entry.get("size_bytes"), "sha256": entry.get("sha256")}
    )
    if (
        size <= 0
        or not relative.rsplit(".", 1)[0].endswith(digest)
        or entry.get("state") not in ("planned", "verified")
    ):
        raise _denied()
    _uuid(entry.get("write_nonce"))
    return size, digest


def _owns(metadata, lease, entry):
    facts = metadata.get("Metadata")
    if (
        not isinstance(facts, dict)
        or facts.get("aap-prediction-lease") != str(lease)
        or facts.get("aap-prediction-write") != entry["write_nonce"]
        or "container" in entry
        and metadata.get("ContentType") != entry["media_type"]
    ):
        raise _mismatch()


def _check(client, pin, lease, entry, *, etag=None):
    params = {"Bucket": pin.bucket, "Key": pin.key}
    if pin.version is not None:
        params["VersionId"] = pin.version
    size = entry["size_bytes"]
    head = client.head_object(**params)
    _owns(head, lease, entry)
    etag, version = _identity(head, size, etag=etag, version=pin.version)
    if version is None:
        raise SavedIOError(
            "prediction_input_immutable_version_required", status_code=503
        )
    exact = params | {"VersionId": version}
    response = client.get_object(**exact, IfMatch=etag)
    body = response["Body"]
    try:
        _owns(response, lease, entry)
        if _identity(response, size, etag=etag, version=version) != (etag, version):
            raise _mismatch()
        _digest(body, size, entry["sha256"])
    finally:
        try:
            body.close()
        except Exception:
            raise SavedIOError(
                "source_close_unsettled", status_code=503, unsettled=True
            ) from None
    after = client.head_object(**exact)
    _owns(after, lease, entry)
    if _identity(after, size, etag=etag, version=version) != (etag, version):
        raise _mismatch()
    return {"etag": etag, "version_id": version}


def _list(client, pin, inventory, *, versions):
    observed, marker, seen = {}, {}, set()
    while True:
        params = {"Bucket": pin.bucket, "Prefix": pin.key, "MaxKeys": 1000, **marker}
        page = (
            client.list_object_versions(**params)
            if versions
            else client.list_objects_v2(**params)
        )
        if (
            type(page.get("IsTruncated")) is not bool
            or versions
            and page.get("DeleteMarkers")
        ):
            raise _mismatch()
        for value in page.get("Versions" if versions else "Contents", ()):
            key = value["Key"]
            relative = key[len(pin.key) :]
            entry = inventory.get(relative)
            if not key.startswith(pin.key) or entry is None or relative in observed:
                raise _mismatch()
            if value.get("Size") != entry["size_bytes"] or not value.get("ETag"):
                raise _mismatch()
            version = value.get("VersionId")
            if versions and (not version or version == "null"):
                raise _mismatch()
            if entry["state"] == "verified" and (
                value["ETag"] != entry["etag"]
                or versions
                and version != entry["version_id"]
            ):
                raise _mismatch()
            observed[relative] = {"etag": value["ETag"], "version_id": version}
        if not page["IsTruncated"]:
            return observed
        marker = (
            {
                "KeyMarker": page.get("NextKeyMarker"),
                "VersionIdMarker": page.get("NextVersionIdMarker"),
            }
            if versions
            else {"ContinuationToken": page.get("NextContinuationToken")}
        )
        cursor = tuple(marker.values())
        if (
            not all(isinstance(value, str) and value for value in cursor)
            or cursor in seen
            or len(seen) > len(inventory)
        ):
            raise _mismatch()
        seen.add(cursor)


def _missing(client, params):
    try:
        client.head_object(**params)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code")) in _MISSING:
            return True
        raise
    return False


def _plan(client, pin, identifier, inventory):
    if len(inventory) > 10_000:
        raise _denied()
    for name, entry in inventory.items():
        _member(entry, name)
    visible = _list(client, pin, inventory, versions=False)
    versions = _list(client, pin, inventory, versions=True)
    if not visible.keys() <= versions.keys():
        raise _mismatch()
    for relative, receipt in versions.items():
        target = type(pin)(pin._target, pin.key + relative, receipt["version_id"])
        if (
            _check(
                client, target, identifier, inventory[relative], etag=receipt["etag"]
            )
            != receipt
        ):
            raise _mismatch()
    return {
        relative: facts | {"current": relative in visible}
        for relative, facts in versions.items()
    }


def _fingerprint(plan):
    return sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _delete(client, pin, inventory, plan):
    evidence = {}
    for relative, receipt in plan.items():
        key = pin.key + relative
        before = receipt["current"]
        params = {"Bucket": pin.bucket, "Key": key}
        exact = params | {"VersionId": receipt["version_id"]}
        try:
            result = client.delete_object(**exact, IfMatch=receipt["etag"])
            if result.get("ResponseMetadata", {}).get("HTTPStatusCode") not in {
                200,
                204,
            } or not _missing(client, exact):
                raise SavedIOError(
                    "prediction_input_cleanup_unproven", status_code=503, unsettled=True
                )
            gone = _missing(client, params)
        except Exception as exc:
            raise physical_error(exc, mutation=True) from None
        size = inventory[relative]["size_bytes"]
        evidence[relative] = {
            "version_id": receipt["version_id"],
            "physical_outcome": "deleted",
            "version_removed": True,
            "current_before": before,
            "current_absent": gone,
            "physical_size_bytes": size,
            "current_delta_bytes": -size if before and gone else None,
        }
    if _list(client, pin, inventory, versions=False) or _list(
        client, pin, inventory, versions=True
    ):
        raise SavedIOError(
            "prediction_input_cleanup_unproven", status_code=503, unsettled=True
        )
    return evidence


def _handle(action):
    async def handle(context, subject, body=None):
        if action in ("plan", "delete"):
            _fields(subject, "id", "permit", *(("plan",) if action == "delete" else ()))
            relative = None
        else:
            _fields(subject, "id", "permit", "relative")
            relative = subject["relative"]
            if type(relative) is not str or not _RELATIVE.fullmatch(relative):
                raise _denied()
        identifier, permit = _uuid(subject["id"]), _uuid(subject["permit"])

        async def proof(claim=False):
            return await context.fetch(
                _SQL,
                identifier,
                permit,
                "delete" if action == "plan" else action,
                relative,
                claim,
            )

        expected = await proof()
        pin = await _pin(context, expected)
        if action in ("plan", "delete"):
            try:
                async with pin.client() as client:
                    plan = await context.run(
                        _plan, client, pin, identifier, expected["inventory"]
                    )
                    digest = _fingerprint(plan)
                    if action == "plan":
                        if await proof() != expected:
                            raise _denied()
                        return {
                            "plan": digest,
                            "versions": {
                                name: value["version_id"]
                                for name, value in plan.items()
                            },
                        }
                    if subject["plan"] != digest or await proof(claim=True) != expected:
                        raise _denied()
                    evidence = await context.run(
                        _delete, client, pin, expected["inventory"], plan
                    )
            except Exception as exc:
                raise physical_error(exc) from None
            if await proof() != expected:
                raise SavedIOError(
                    "prediction_input_cleanup_unproven", status_code=503, unsettled=True
                )
            return {"removed": evidence}
        entry = expected["member"]
        size, digest = _member(entry, relative)
        written = None
        reused = False
        if action == "write":
            bucket = await pin.get_bucket_versioning()
            if bucket.get("Status") != "Enabled":
                raise SavedIOError(
                    "prediction_input_bucket_versioning_required", status_code=503
                )
            await context.run(_digest, body, size, digest)
            body.seek(0)
            if await proof(claim=True) != expected:
                raise _denied()
            try:
                written = await pin.put(
                    body,
                    length=size,
                    content_type=entry["media_type"],
                    if_none_match="*",
                    content_fields={"CacheControl": "private, max-age=3600, immutable"},
                    metadata={
                        "aap-prediction-lease": str(identifier),
                        "aap-prediction-write": entry["write_nonce"],
                    },
                )
            except SavedIOError as exc:
                if exc.unsettled or exc.storage_code not in {
                    "412",
                    "PreconditionFailed",
                }:
                    raise
                reused = True
            if written is not None and (
                not written.get("VersionId")
                or written["VersionId"] == "null"
                or not written.get("ETag")
            ):
                raise SavedIOError(
                    "prediction_input_write_unproven", status_code=503, unsettled=True
                )
        version = written["VersionId"] if written is not None else entry["version_id"]
        etag = written["ETag"] if written is not None else entry["etag"]
        try:
            async with pin.client() as client:
                receipt = await context.run(
                    _check,
                    client,
                    pin.with_version(version),
                    identifier,
                    entry,
                    etag=etag,
                )
        except Exception as exc:
            raise physical_error(exc) from None
        if await proof() != expected:
            raise _denied()
        seconds = min(
            3600,
            int(
                (
                    datetime.fromisoformat(expected["expires_at"])
                    - datetime.now(timezone.utc)
                ).total_seconds()
            ),
        )
        if seconds <= 0:
            raise _denied()
        current = False
        if written is not None:
            try:
                observed = await pin.head()
                current = _identity(
                    observed, size, etag=receipt["etag"], version=receipt["version_id"]
                ) == (
                    receipt["etag"],
                    receipt["version_id"],
                )
            except SavedIOError as exc:
                if exc.unsettled:
                    raise
        return {
            **receipt,
            "current_head_matches": current,
            "reused": reused,
            "url": await pin.with_version(receipt["version_id"]).sign(
                "GET", expires_in=seconds
            ),
        }

    return handle


def register(registry):
    registry.allow(_SQL + "(uuid,uuid,text,text,boolean)")
    registry.add(
        _FAMILY, "prediction-input-write", "upload", _handle("write"), access="write"
    )
    registry.add(
        _FAMILY,
        "prediction-input-reference",
        "command",
        _handle("reference"),
        access="read",
    )
    registry.add(
        _FAMILY,
        "prediction-input-delete-plan",
        "command",
        _handle("plan"),
        access="cleanup",
    )
    registry.add(
        _FAMILY,
        "prediction-input-delete",
        "command",
        _handle("delete"),
        access="cleanup",
    )
