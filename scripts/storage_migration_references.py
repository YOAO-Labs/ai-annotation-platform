"""Private, bounded saved-reference reports; never a copy, deletion or cutover permit."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
from urllib.parse import urlsplit

from storage_migration_inventory import PURPOSES, SCHEMA as SOURCE_SCHEMA

SCHEMA = "aap.storage-migration-references/v1"
MAX_RECORD = 256 * 1024
SHA = re.compile(r"^[0-9a-f]{64}$")
REF_FIELDS = (
    "schema",
    "purpose",
    "location_id",
    "config_revision",
    "binding_generation",
    "endpoint_url",
    "bucket",
    "object_key",
    "prefix",
    "key_prefix",
    "storage_identity",
)


class ReferenceError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Limits:
    page_size: int = 500
    max_pages: int = 10000
    max_rows: int = 1000000
    max_expanded: int = 1000000
    max_output_bytes: int = 256 * 1024**2
    max_source_bytes: int = 1024**3
    max_source_entries: int = 1000000
    max_seconds: float = 300
    statement_seconds: float = 15

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name.endswith("seconds"):
                valid = (
                    type(value) in (int, float) and math.isfinite(value) and value > 0
                )
            else:
                valid = type(value) is int and value > 0
            if not valid:
                raise ReferenceError("invalid_limits")
        if self.page_size > 1000 or self.max_output_bytes < 65536:
            raise ReferenceError("invalid_limits")


class Budget:
    def __init__(self, limits):
        self.limits = limits
        self.started = time.monotonic()
        self.rows = self.pages = self.expanded = 0

    def check(self):
        if time.monotonic() - self.started > self.limits.max_seconds:
            raise ReferenceError("time_budget")

    def take(self, kind, amount=1):
        self.check()
        maximum = getattr(self.limits, "max_" + kind)
        value = getattr(self, kind) + amount
        if value > maximum:
            raise ReferenceError(kind + "_budget")
        setattr(self, kind, value)


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    try:
        data = (
            json.dumps(
                value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            + b"\n"
        )
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ReferenceError("invalid_record") from None
    if len(data) > MAX_RECORD:
        raise ReferenceError("record_budget")
    return data


def private_open(path):
    return os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")


class Writer:
    def __init__(self, path, maximum):
        self.stream = private_open(path)
        self.maximum = maximum
        self.digest = hashlib.sha256()
        self.count = self.size = 0

    def emit(self, value, *, final=False):
        data = canonical(value)
        if self.size + len(data) > self.maximum - (0 if final else 32768):
            raise ReferenceError("output_byte_budget")
        self.stream.write(data)
        self.digest.update(data)
        self.count += 1
        self.size += len(data)

    def finish(self, summary):
        self.emit({"type": "summary", **summary}, final=True)
        result = {
            "type": "tail",
            "schema": SCHEMA,
            "prior_records": self.count,
            "prior_bytes": self.size,
            "sha256": self.digest.hexdigest(),
            **summary,
        }
        data = canonical(result)
        if self.size + len(data) > self.maximum:
            raise ReferenceError("output_byte_budget")
        self.stream.write(data)
        self.stream.flush()
        os.fsync(self.stream.fileno())
        return result


def framed(stream, schema, *, budget=None, max_bytes=None):
    """Read and verify one descriptor; callers may spool records before accepting its tail."""
    digest, full = hashlib.sha256(), hashlib.sha256()
    count = size = 0
    summary = tail = None
    while True:
        if budget:
            budget.check()
        line = stream.readline(MAX_RECORD + 1)
        if not line:
            break
        if len(line) > MAX_RECORD or not line.endswith(b"\n") or tail is not None:
            raise ReferenceError("invalid_report")
        full.update(line)
        if max_bytes is not None and size + len(line) > max_bytes:
            raise ReferenceError("source_byte_budget")
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            raise ReferenceError("invalid_report") from None
        if not isinstance(record, dict) or canonical(record) != line:
            raise ReferenceError("invalid_report")
        if count == 0 and (
            record.get("type") != "header" or record.get("schema") != schema
        ):
            raise ReferenceError("invalid_report")
        if count and record.get("type") == "header":
            raise ReferenceError("invalid_report")
        if record.get("type") == "tail":
            tail = record
        else:
            if summary is not None:
                raise ReferenceError("invalid_report")
            if record.get("type") == "summary":
                summary = {k: v for k, v in record.items() if k != "type"}
            digest.update(line)
            count += 1
            size += len(line)
            yield record
    if tail is None or summary is None:
        raise ReferenceError("unsealed_report")
    if (
        tail.get("schema") != schema
        or tail.get("prior_records") != count
        or tail.get("prior_bytes") != size
        or tail.get("sha256") != digest.hexdigest()
        or {
            k: v
            for k, v in tail.items()
            if k not in {"type", "schema", "prior_records", "prior_bytes", "sha256"}
        }
        != summary
        or type(summary.get("complete")) is not bool
        or summary.get("migration_ready") is not False
    ):
        raise ReferenceError("integrity_mismatch")
    yield {"type": "verified_tail", **summary, "file_sha256": full.hexdigest()}


def verify(path, limits=None):
    limits = limits or Limits()
    with Path(path).open("rb") as stream:
        result = None
        for record in framed(
            stream, SCHEMA, budget=Budget(limits), max_bytes=limits.max_output_bytes
        ):
            result = record
    return result


def endpoint(value):
    """Endpoint identity only; object keys never pass through URL parsing."""
    if (
        not isinstance(value, str)
        or len(value.encode()) > 8192
        or any(ord(c) < 32 for c in value)
    ):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or "\\" in value
        ):
            return None
        return (
            parts.scheme,
            parts.hostname.lower(),
            parts.port or (443 if parts.scheme == "https" else 80),
            parts.path.rstrip("/"),
        )
    except ValueError:
        return None


def identity(address, bucket):
    return hashlib.sha256(
        json.dumps([address.rstrip("/"), bucket]).encode()
    ).hexdigest()


def version_kind(value):
    return (
        "unpinned"
        if value is None
        else ("mutable_null" if value == "null" else "explicit_version")
    )


class SourceIndex:
    def __init__(self, path, source, budget):
        self.db = None
        self.budget = budget
        self.header = self.summary = None
        self.count = 0
        self.buckets = set()
        with private_open(path):
            pass
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.set_progress_handler(
            lambda: int(time.monotonic() - budget.started > budget.limits.max_seconds),
            1000,
        )
        self.db.execute(
            "CREATE TABLE objects(bucket TEXT,key TEXT,version TEXT,kind TEXT,data TEXT,matched INTEGER DEFAULT 0,PRIMARY KEY(bucket,key,version,kind))"
        )
        self.db.execute("CREATE INDEX by_key ON objects(bucket,key)")
        try:
            # No path reopen: verification, indexing and full-file digest use this FD.
            with Path(source).open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ReferenceError("source_not_regular")
                for record in framed(
                    stream,
                    SOURCE_SCHEMA,
                    budget=budget,
                    max_bytes=budget.limits.max_source_bytes,
                ):
                    self._record(record)
                after = os.fstat(stream.fileno())
                if tuple(
                    getattr(before, k)
                    for k in (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                ) != tuple(
                    getattr(after, k)
                    for k in (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                ):
                    raise ReferenceError("source_changed")
                self.file_identity = {
                    k: getattr(after, k)
                    for k in (
                        "st_dev",
                        "st_ino",
                        "st_size",
                        "st_mtime_ns",
                        "st_ctime_ns",
                    )
                }
            self.db.commit()
        except sqlite3.OperationalError as exc:
            self.close()
            raise ReferenceError(
                "time_budget"
                if exc.sqlite_errorcode == sqlite3.SQLITE_INTERRUPT
                else "source_index_failed"
            ) from None
        except BaseException:
            self.close()
            raise

    def _record(self, row):
        kind = row["type"]
        if kind == "header":
            address = row.get("source", {}).get("endpoint")
            if endpoint(address) is None:
                raise ReferenceError("source_endpoint_unknown")
            self.header = {"endpoint": address, "started_at": row.get("started_at")}
            purposes = row.get("purpose_buckets")
            if (
                not isinstance(purposes, dict)
                or set(purposes) != PURPOSES
                or any(not isinstance(v, str) for v in purposes.values())
            ):
                raise ReferenceError("invalid_source")
            self.buckets = set(purposes.values())
        elif kind == "verified_tail":
            self.summary = row
            self.header["finished_at"] = row.get("finished_at")
        elif kind in {"object", "delete_marker"}:
            self.count += 1
            if self.count > self.budget.limits.max_source_entries:
                raise ReferenceError("source_entry_budget")
            bucket, key, version = (
                row.get("bucket"),
                row.get("key"),
                row.get("version_id"),
            )
            if (
                not isinstance(bucket, str)
                or bucket not in self.buckets
                or not isinstance(key, str)
                or (
                    version is not None
                    and (not isinstance(version, str) or not version)
                )
            ):
                raise ReferenceError("invalid_source_object")
            verification = row.get("verification") or {}
            if kind == "object" and (
                type(row.get("size_bytes")) is not int
                or row["size_bytes"] < 0
                or not isinstance(row.get("etag"), str)
                or verification.get("status") not in {"verified", "unverified"}
                or (
                    verification.get("status") == "verified"
                    and (
                        not isinstance(verification.get("sha256"), str)
                        or not SHA.fullmatch(verification["sha256"])
                    )
                )
            ):
                raise ReferenceError("invalid_source_object")
            projected = {
                "sha256": verification.get("sha256"),
                "size_bytes": row.get("size_bytes"),
                "verification": verification.get("status"),
                "etag": row.get("etag"),
            }
            try:
                self.db.execute(
                    "INSERT INTO objects(bucket,key,version,kind,data) VALUES(?,?,?,?,?)",
                    (bucket, key, json.dumps(version), kind, json.dumps(projected)),
                )
            except sqlite3.IntegrityError:
                raise ReferenceError("source_duplicate_identity") from None

    def mark(self, ref):
        self.db.execute(
            "UPDATE objects SET matched=1 WHERE bucket=? AND key=? AND version=? AND kind='object'",
            (ref["bucket"], ref["key"], json.dumps(ref["version_id"])),
        )

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def match(self, ref):
        self.budget.check()
        if ref.get("unknown"):
            return {"status": "unlocated", "code": ref["unknown"]}
        address, bucket = ref.get("endpoint_url"), ref.get("bucket")
        if endpoint(address) is None or not isinstance(bucket, str):
            return {"status": "unlocated", "code": "address_unknown"}
        if (
            endpoint(address) != endpoint(self.header["endpoint"])
            or bucket not in self.buckets
        ):
            return {"status": "outside_selected_source"}
        if ref.get("prefix") is not None:
            prefix = ref["prefix"]
            if not isinstance(prefix, str) or not prefix or not prefix.endswith("/"):
                return {"status": "unlocated", "code": "prefix_boundary_unknown"}
            count = self.db.execute(
                "SELECT count(*) FROM objects WHERE bucket=? AND kind='object' AND substr(CAST(key AS BLOB),1,length(CAST(? AS BLOB)))=CAST(? AS BLOB)",
                (bucket, prefix, prefix),
            ).fetchone()[0]
            return {
                "status": "prefix_scope" if count else "not_observed",
                "observed_objects": count,
                "exact_object_membership": False,
            }
        key = ref.get("key")
        if not isinstance(key, str) or not key:
            return {"status": "unlocated", "code": "key_unknown"}
        version = ref.get("version_id")
        if version is None:
            count = self.db.execute(
                "SELECT count(*) FROM objects WHERE bucket=? AND key=? AND kind='object'",
                (bucket, key),
            ).fetchone()[0]
            return {
                "status": "unpinned_key" if count else "not_observed",
                "observed_versions": count,
            }
        found = self.db.execute(
            "SELECT kind,data FROM objects WHERE bucket=? AND key=? AND version=?",
            (bucket, key, json.dumps(version)),
        ).fetchall()
        if not found:
            return {"status": "not_observed"}
        if len(found) != 1 or found[0][0] != "object":
            return {"status": "conflict", "code": "deleted_or_conflicting_version"}
        data = json.loads(found[0][1])
        if data["verification"] != "verified":
            return {"status": "source_unverified"}
        if any(
            ref.get(k) is not None and ref[k] != data[k]
            for k in ("sha256", "size_bytes")
        ):
            return {"status": "conflict", "code": "saved_content_differs"}
        return {
            "status": "mutable_null" if version == "null" else "exact_version",
            "source_sha256": data["sha256"],
            "source_size_bytes": data["size_bytes"],
            "source_etag": data["etag"],
        }


def address_reference(ref, locations, source_address):
    """Use the saved address first; history and today's bindings are never fallbacks."""
    ref = dict(ref)
    for name in (
        "endpoint_url",
        "bucket",
        "key",
        "prefix",
        "key_prefix",
        "storage_identity",
        "version_id",
    ):
        if ref.get(name) is not None and not isinstance(ref[name], str):
            ref.pop(name)
            ref.setdefault("unknown", "invalid_locator_field")
    for name in ("sha256", "canonical_representation_sha256"):
        if ref.get(name) is not None and (
            not isinstance(ref[name], str) or not SHA.fullmatch(ref[name])
        ):
            ref.pop(name)
            ref.setdefault("unknown", "invalid_content_receipt")
    if ref.get("size_bytes") is not None and (
        type(ref["size_bytes"]) is not int or ref["size_bytes"] < 0
    ):
        ref.pop("size_bytes")
        ref.setdefault("unknown", "invalid_content_receipt")
    if ref.get("key") is not None and ref.get("prefix") is not None:
        ref.setdefault("unknown", "conflicting_locator_kinds")
    location = locations.get(ref.get("location_id"))
    current = location.get("endpoint_url") if location else None
    saved = ref.get("storage_identity")
    ref["registered_location_check"] = (
        "missing"
        if location is None
        else "identity_not_saved"
        if not saved
        else "matches_saved_identity"
        if current and identity(current, ref.get("bucket", "")) == saved
        else "identity_differs_or_unavailable"
    )
    if ref.get("endpoint_url") is not None:
        if endpoint(ref["endpoint_url"]) is None:
            ref.pop("endpoint_url", None)
            ref.setdefault("unknown", "invalid_saved_endpoint")
        else:
            ref["address_basis"] = "saved_endpoint"
            saved = ref.get("storage_identity")
            if saved is not None and saved != identity(
                ref["endpoint_url"], ref.get("bucket", "")
            ):
                ref.setdefault("unknown", "saved_identity_conflict")
        return ref
    if ref.get("unknown"):
        return ref
    location = locations.get(ref.get("location_id"), {})
    candidates = {
        value
        for value in (location.get("endpoint_url"), source_address)
        if endpoint(value)
    }
    saved = ref.get("storage_identity")
    matches = [
        value
        for value in candidates
        if saved and identity(value, ref.get("bucket", "")) == saved
    ]
    if len(matches) == 1:
        ref["endpoint_url"], ref["address_basis"] = (
            matches[0],
            "saved_identity_matches_known_endpoint",
        )
    elif (
        not saved
        and ref.get("role") in {"current_locator", "preview_locator"}
        and endpoint(location.get("endpoint_url"))
    ):
        ref["endpoint_url"], ref["address_basis"] = (
            location["endpoint_url"],
            "current_registered_location_only",
        )
    else:
        ref.setdefault(
            "unknown", "endpoint_unlocated" if not matches else "endpoint_ambiguous"
        )
    return ref


def write_catalog(output, spool, metadata, source, directory, budget):
    try:
        index = SourceIndex(Path(directory) / "source.sqlite", source, budget)
    except ReferenceError as exc:
        if "budget" not in exc.code:
            raise
        writer = Writer(output, budget.limits.max_output_bytes)
        try:
            writer.emit(
                {
                    "type": "header",
                    "schema": SCHEMA,
                    "scope": "registered-saved-s3-owner-families",
                    "database": metadata,
                    "source_report": {"verified": False},
                    "migration_ready": False,
                }
            )
            writer.emit({"type": "issue", "code": exc.code})
            writer.emit(
                {
                    "type": "unexpanded",
                    "document": "saved_reference_snapshot",
                    "code": "source_join_not_completed",
                }
            )
            return writer.finish(
                {
                    "complete": False,
                    "enumeration_complete": metadata["enumeration_complete"],
                    "resolution_complete": False,
                    "counts": {
                        "references": 0,
                        "unexpanded": 1,
                        "issues": 1,
                        "resolution_unknown": 1,
                        "conflicts": 0,
                    },
                    "migration_ready": False,
                    "finished_at": now(),
                }
            )
        finally:
            writer.stream.close()
    writer = None
    try:
        writer = Writer(output, budget.limits.max_output_bytes)
        counts = {
            "references": 0,
            "unexpanded": 0,
            "issues": 0,
            "resolution_unknown": 0,
            "conflicts": 0,
        }
        matches, locations = {}, {}
        enumeration = metadata["enumeration_complete"]
        writer.emit(
            {
                "type": "header",
                "schema": SCHEMA,
                "started_at": now(),
                "scope": "registered-saved-s3-owner-families",
                "database": metadata,
                "source_report": {
                    **index.header,
                    "file_identity": index.file_identity,
                    "file_sha256": index.summary["file_sha256"],
                    "complete": index.summary["complete"],
                },
                "limits": asdict(budget.limits),
                "migration_ready": False,
                "atomic_snapshot": False,
                "external_writers": "unknown",
                "presigned_uploads": "unknown",
                "stop_coverage": "unknown",
                "legacy_embedded_business_documents": "not_expanded",
            }
        )
        try:
            with Path(spool).open("rb") as stream:
                while line := stream.readline(MAX_RECORD + 1):
                    budget.check()
                    if len(line) > MAX_RECORD:
                        raise ReferenceError("record_budget")
                    row = json.loads(line)
                    kind = row["type"]
                    if kind == "location":
                        locations[row["location_id"]] = row
                    elif kind == "reference":
                        row = address_reference(
                            row, locations, index.header["endpoint"]
                        )
                        row["version_kind"] = version_kind(row.get("version_id"))
                        row["match"] = index.match(row)
                    writer.emit(row)
                    if kind == "reference":
                        counts["references"] += 1
                        status = row["match"]["status"]
                        matches[status] = matches.get(status, 0) + 1
                        counts["resolution_unknown"] += int(
                            status not in {"exact_version", "mutable_null"}
                        )
                        counts["conflicts"] += int(status == "conflict")
                        if status in {"exact_version", "mutable_null"}:
                            index.mark(row)
                    elif kind == "unexpanded":
                        counts["unexpanded"] += 1
                    elif kind == "issue":
                        counts["issues"] += 1
        except (ReferenceError, sqlite3.OperationalError) as exc:
            enumeration = False
            counts["issues"] += 1
            code = (
                exc.code
                if isinstance(exc, ReferenceError)
                else "join_time_or_index_failure"
            )
            writer.emit({"type": "issue", "code": code}, final=True)
        index.db.set_progress_handler(None, 0)
        unmatched = index.db.execute(
            "SELECT count(*) FROM objects WHERE kind='object' AND matched=0"
        ).fetchone()[0]
        return writer.finish(
            {
                "complete": enumeration
                and not any(
                    counts[k] for k in ("resolution_unknown", "issues", "unexpanded")
                )
                and index.summary["complete"],
                "enumeration_complete": enumeration,
                "resolution_complete": not any(
                    counts[k] for k in ("resolution_unknown", "unexpanded", "issues")
                ),
                "counts": counts,
                "matches": matches,
                "source_objects_without_exact_match": unmatched,
                "no_match_meaning": "no exact match in this bounded catalog; never orphan or deletion evidence",
                "migration_ready": False,
                "finished_at": now(),
            }
        )
    finally:
        if writer:
            writer.stream.close()
        index.close()
