"""Observe managed ZIP uploads without changing their SDK transport or retries."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import threading
import time

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import media_cache_accounting, storage_access

MAX_PAGES = 10
MAX_ENTRIES = 2000
OBSERVE_SECONDS = 15
FINAL_OPERATIONS = ("PutObject", "CompleteMultipartUpload")
log = logging.getLogger(__name__)


def recorder(db, output):
    ref = output.storage_ref
    return media_cache_accounting.Recorder(
        async_sessionmaker(storage_access.engine(db), expire_on_commit=False),
        "export_zip_outputs",
        output.id,
        ref["endpoint_url"],
        ref["bucket"],
        purpose="temporary",
    )


async def finish(accounting, failure):
    try:
        await accounting.finish(
            settled=not getattr(failure, "storage_operation_unsettled", False)
        )
    except Exception:
        log.warning("ZIP accounting publication deferred")
    except BaseException as publishing:
        if failure is None:
            return publishing
        if getattr(publishing, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(failure)
    return failure


def immutable(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 1024
        and value != "null"
        and not any(ord(char) < 32 or ord(char) == 127 for char in value)
    )


def _status(response):
    return response.get("ResponseMetadata", {}).get("HTTPStatusCode")


def _head(client, bucket, key):
    try:
        response = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        response = getattr(exc, "response", {})
        headers = response.get("ResponseMetadata", {}).get("HTTPHeaders", {})
        if _status(response) != 404 or str(
            response.get("Error", {}).get("Code")
        ) not in {
            "404",
            "NotFound",
            "NoSuchKey",
        }:
            raise ValueError("unknown_head") from None
        marker = (
            response.get("DeleteMarker") is True
            or str(headers.get("x-amz-delete-marker", "false")).lower() == "true"
        )
        version = response.get("VersionId") or headers.get("x-amz-version-id")
        if marker and immutable(version):
            return ("marker", version)
        if (
            not marker
            and version is None
            and response.get("DeleteMarker") in (None, False)
        ):
            return None
        raise ValueError("unknown_absence") from None
    if (
        _status(response) != 200
        or not immutable(response.get("VersionId"))
        or response.get("DeleteMarker", False) is not False
        or type(response.get("ContentLength")) is not int
        or response["ContentLength"] < 0
        or not isinstance(response.get("ETag"), str)
        or not response["ETag"]
    ):
        raise ValueError("unknown_head")
    return (
        "object",
        response["VersionId"],
        response["ContentLength"],
        response["ETag"],
    )


@dataclass(frozen=True)
class Snapshot:
    versions: dict
    current: tuple | None

    @property
    def size(self):
        return self.current[2] if self.current and self.current[0] == "object" else 0

    @property
    def evidence(self):
        raw = json.dumps(sorted(self.versions.items()), separators=(",", ":"))
        return {
            "version_set_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "version_count": len(self.versions),
            "current": self.current,
        }


def snapshot(client, bucket, key):
    """A bounded complete exact-key set, corroborated by both current HEADs."""
    try:
        deadline = time.monotonic() + OBSERVE_SECONDS
        before = _head(client, bucket, key)
        versions, latest, markers, seen, count = {}, [], {}, set(), 0
        for _ in range(MAX_PAGES):
            if time.monotonic() >= deadline:
                return None
            response = client.list_object_versions(
                Bucket=bucket, Prefix=key, MaxKeys=200, **markers
            )
            if (
                _status(response) != 200
                or type(response.get("IsTruncated")) is not bool
            ):
                return None
            for field, kind in (("Versions", "object"), ("DeleteMarkers", "marker")):
                rows = response.get(field, [])
                if not isinstance(rows, list):
                    return None
                count += len(rows)
                if count > MAX_ENTRIES:
                    return None
                for row in rows:
                    if not isinstance(row, dict):
                        return None
                    if row.get("Key") != key:
                        continue
                    version, modified = row.get("VersionId"), row.get("LastModified")
                    if (
                        not immutable(version)
                        or version in versions
                        or not isinstance(modified, datetime)
                        or modified.utcoffset() is None
                        or type(row.get("IsLatest")) is not bool
                    ):
                        return None
                    if kind == "object":
                        if (
                            type(row.get("Size")) is not int
                            or row["Size"] < 0
                            or not isinstance(row.get("ETag"), str)
                            or not row["ETag"]
                        ):
                            return None
                        current = (kind, version, row["Size"], row["ETag"])
                    else:
                        current = (kind, version)
                    versions[version] = (*current, modified.isoformat())
                    if row["IsLatest"]:
                        latest.append(current)
            if not response["IsTruncated"]:
                break
            pair = response.get("NextKeyMarker"), response.get("NextVersionIdMarker")
            if (
                any(not isinstance(item, str) or not item for item in pair)
                or pair in seen
            ):
                return None
            seen.add(pair)
            markers = dict(zip(("KeyMarker", "VersionIdMarker"), pair))
        else:
            return None
        after = _head(client, bucket, key)
        if time.monotonic() >= deadline or before != after:
            return None
        if not versions:
            return Snapshot(versions, None) if before is None else None
        if len(latest) != 1 or latest[0] != before:
            return None
        return Snapshot(versions, before)
    except Exception:
        return None


class Observation:
    def __init__(self, accounting, client, key, receipt):
        self.client, self.bucket, self.key = client, accounting.bucket, key
        self.attempt = accounting.begin(key, receipt)
        self.attempt.evidence.update(final_send_count=0, hooks_complete=False)
        self.lock = threading.Lock()
        self.params, self.sends, self.acks, self.opaque = {}, 0, 0, False
        self.before = self.ack = self.read_version = None

    def parameters(self, params, model, **kwargs):
        with self.lock:
            valid = (
                params.get("Bucket") == self.bucket and params.get("Key") == self.key
            )
            self.params[model.name] = valid
            self.opaque = self.opaque or not valid

    def sending(self, event_name, **kwargs):
        operation = event_name.rsplit(".", 1)[-1]
        with self.lock:
            self.sends += 1
            self.attempt.evidence["final_send_count"] = self.sends
            first = self.sends == 1
            self.opaque = self.opaque or not self.params.get(operation, False)
        if first:
            self.before = snapshot(self.client, self.bucket, self.key)
            if self.before is not None:
                self.attempt.evidence["before"] = self.before.evidence

    def returned(self, parsed, model, http_response=None, **kwargs):
        if (
            isinstance(parsed, dict)
            and _status(parsed) == 200
            and getattr(http_response, "status_code", None) == 200
            and not parsed.get("Error")
        ):
            with self.lock:
                self.acks += 1
                if not self.params.get(model.name, False):
                    self.opaque = True
                self.ack = dict(parsed)
            self.attempt.written(parsed)

    @contextmanager
    def hooks(self):
        events = getattr(getattr(self.client, "meta", None), "events", None)
        registered = []
        try:
            for operation in FINAL_OPERATIONS:
                for event, callback in (
                    ("before-parameter-build", self.parameters),
                    ("before-send", self.sending),
                    ("after-call", self.returned),
                ):
                    name = f"{event}.s3.{operation}"

                    def observe(_call=callback, **kwargs):
                        try:
                            _call(**kwargs)
                        except Exception:
                            self.opaque = True
                        # Public SDK hooks can override the response when non-None.
                        return None

                    events.register(name, observe)
                    registered.append((name, observe))
        except Exception:
            self.opaque = True
        try:
            yield self
        finally:
            for name, callback in registered:
                try:
                    events.unregister(name, callback)
                except Exception:
                    self.opaque = True

    def readback(self, response):
        if (
            _status(response) == 200
            and not response.get("DeleteMarker")
            and response.get("ContentRange") is None
            and response.get("ContentLength") == self.attempt.receipt["size_bytes"]
        ):
            self.read_version = response.get("VersionId")

    def complete(self):
        after = snapshot(self.client, self.bucket, self.key)
        evidence = self.attempt.evidence
        evidence.update(
            final_send_count=self.sends,
            hooks_complete=not self.opaque and self.sends >= 1 and self.acks >= 1,
        )
        if self.before is not None:
            evidence["before"] = self.before.evidence
        if after is not None:
            evidence["after"] = after.evidence
        returned = (self.ack or {}).get("VersionId")
        if (
            self.opaque
            or self.sends != 1
            or self.acks != 1
            or self.before is None
            or after is None
            or not immutable(returned)
            or self.read_version != returned
            or after.current is None
            or after.current[0] != "object"
            or after.current[1:3] != (returned, self.attempt.receipt["size_bytes"])
            or any(
                after.versions.get(version) != row
                for version, row in self.before.versions.items()
            )
            or set(after.versions) - set(self.before.versions) != {returned}
        ):
            return
        evidence.update(
            full_readback=True,
            current_head_matches=True,
            current_delta_bytes=after.size - self.before.size,
        )
