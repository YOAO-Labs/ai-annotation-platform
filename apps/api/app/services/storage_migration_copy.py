"""Bounded initial S3 copy with durable, private per-version evidence.

This tool never changes bindings, deletes objects, or authorizes migration.
Checkpoint digests detect accidental changes, not malicious forgery.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import tempfile
from dataclasses import asdict, dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
import uuid

from app.services.storage_migration_inventory import PURPOSES
from app.services.storage_migration_references import (
    Budget as ReferenceBudget,
    Limits as ReferenceLimits,
    ReferenceError,
    SCHEMA as REFERENCE_SCHEMA,
    SourceIndex,
    canonical,
    endpoint,
    framed,
    now,
)

SCHEMA = "aap.storage-migration-copy/v1"
GENERATION_SCHEMA = "aap.storage-migration-copy/v2"
GENERATION_COUNTS = (
    "reused_objects",
    "new_objects",
    "mutable_recopies",
    "source_identity_conflicts",
    "previous_only_objects",
    "delete_markers",
    "history_records",
)
CHUNK = 1024 * 1024
STRICT_CLOSE = ContextVar("migration_copy_strict_close", default=False)
CONTENT_FIELDS = (
    "ContentType",
    "CacheControl",
    "ContentDisposition",
    "ContentEncoding",
    "ContentLanguage",
    "Expires",
    "Metadata",
)
RESERVED = {"aap-copy-plan", "aap-copy-object", "aap-copy-nonce"}


class CopyError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class CopyStopped(BaseException):
    """Application stop or persistence failure must bypass per-object catches."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Paused(CopyError):
    pass


@dataclass(frozen=True)
class Limits:
    max_object_bytes: int = 256 * 1024**2
    max_source_bytes: int = 2 * 1024**3
    max_upload_bytes: int = 2 * 1024**3
    max_verify_bytes: int = 2 * 1024**3
    max_objects: int = 10000
    max_seconds: float = 1800
    connect_timeout_seconds: float = 10
    read_timeout_seconds: float = 60

    def __post_init__(self):
        for name, value in asdict(self).items():
            valid = (
                type(value) in (int, float) and math.isfinite(value) and value > 0
                if name.endswith("seconds")
                else type(value) is int and value > 0
            )
            if not valid:
                raise CopyError("invalid_limits")


class Budget:
    def __init__(self, limits, stopped=lambda: False):
        self.limits, self.stopped = limits, stopped
        self.started = time.monotonic()
        self.source_bytes = self.upload_bytes = self.verify_bytes = self.objects = 0

    def check(self):
        if self.stopped():
            raise Paused("signal_pause")
        if time.monotonic() - self.started > self.limits.max_seconds:
            raise Paused("time_budget")

    def reserve(self, name, amount):
        self.check()
        if amount > getattr(self.limits, "max_" + name) - getattr(self, name):
            raise Paused(name + "_budget")

    def take(self, name, amount):
        self.reserve(name, amount)
        setattr(self, name, getattr(self, name) + amount)


def packed(value):
    return canonical(value).decode().rstrip("\n")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def error_code(exc):
    # Never expose provider errors, URLs, object keys or credentials.
    return exc.code if isinstance(exc, (CopyError, ReferenceError)) else "io_failure"


def private_parent(path):
    info = path.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CopyError("private_parent_required")


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise CopyError("private_json_required")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise CopyError("invalid_private_json")

        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise CopyError("invalid_private_json")
                result[key] = value
            return result

        return json.loads(raw, object_pairs_hook=unique)
    except CopyError:
        raise
    except (ValueError, UnicodeError):
        raise CopyError("invalid_private_json") from None
    finally:
        os.close(fd)


def target_config(path):
    value = private_json(path)
    fields = {
        "endpoint_url",
        "bucket",
        "key_prefix",
        "access_key_id",
        "secret_access_key",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or any(not isinstance(v, str) for v in value.values())
        or endpoint(value["endpoint_url"]) is None
        or not value["bucket"]
        or not value["access_key_id"]
        or not value["secret_access_key"]
        or len(value["bucket"].encode()) > 255
        or len(value["key_prefix"].encode()) > 800
    ):
        raise CopyError("invalid_target_config")
    return value


def target_identity(config):
    return {k: config[k] for k in ("endpoint_url", "bucket", "key_prefix")}


class PlanSource(SourceIndex):
    """Reuse sealed single-descriptor indexing; retain handling and non-body facts."""

    def _record(self, row):
        super()._record(row)
        if row["type"] == "header":
            self.original_header = row
            self.db.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY,data TEXT)")
        elif row["type"] in {"bucket", "multipart", "issue"}:
            self.db.execute("INSERT INTO facts(data) VALUES(?)", (packed(row),))
        elif row["type"] in {"object", "delete_marker"}:
            latest = row.get("is_latest")
            if latest is not None and type(latest) is not bool:
                raise CopyError("invalid_source_is_latest")
            args = (
                row["bucket"],
                row["key"],
                json.dumps(row.get("version_id")),
                row["type"],
            )
            data = self.db.execute(
                "SELECT data FROM objects WHERE bucket=? AND key=? AND version=? AND kind=?",
                args,
            ).fetchone()[0]
            value = json.loads(data) | {"is_latest": latest}
            self.db.execute(
                "UPDATE objects SET data=? WHERE bucket=? AND key=? AND version=? AND kind=?",
                (packed(value), *args),
            )


def physical_intent(row):
    return bool(
        row.get("receipt")
        or (row.get("nonce") and isinstance(row.get("content"), dict))
    )


def history_id(record):
    if record["record_type"] == "fact":
        return "f:" + digest(record["value"])
    row = record["value"]
    # Verification state and receipt timestamps are observations of one creation,
    # not new physical identities. Unwritten mappings are only source facts.
    return "r:" + digest(
        {
            "source": row["source"],
            "source_kind": row["source_kind"],
            "identity": [row.get(k) for k in ("sha256", "size_bytes", "etag")],
            "target_key": row.get("target_key") if physical_intent(row) else None,
        }
    )


def attributes(info):
    return tuple(
        getattr(info, k)
        for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    )


def read_catalog(path, index, budget):
    """Verify and project a catalog from exactly one descriptor."""
    header = summary = None
    counts = {}
    with Path(path).open("rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise CopyError("catalog_not_regular")
        for row in framed(
            stream,
            REFERENCE_SCHEMA,
            budget=budget,
            max_bytes=budget.limits.max_output_bytes,
        ):
            kind = row["type"]
            if kind not in {
                "header",
                "location",
                "binding",
                "reference",
                "unexpanded",
                "issue",
                "summary",
                "verified_tail",
            }:
                raise CopyError("invalid_catalog_record")
            counts[kind] = counts.get(kind, 0) + 1
            if kind == "header":
                header = row
            elif kind == "verified_tail":
                summary = row
            elif kind == "reference":
                if index.match(row)["status"] in {"exact_version", "mutable_null"}:
                    index.mark(row)
        if attributes(before) != attributes(os.fstat(stream.fileno())):
            raise CopyError("catalog_changed")
    source = header.get("source_report", {})
    if (
        source.get("file_sha256") != index.summary["file_sha256"]
        or source.get("endpoint") != index.header["endpoint"]
        or source.get("complete") is not index.summary["complete"]
    ):
        raise CopyError("catalog_source_mismatch")
    catalog_counts = summary.get("counts", {})
    if (
        not isinstance(catalog_counts, dict)
        or not set(catalog_counts)
        <= {"references", "unexpanded", "issues", "resolution_unknown", "conflicts"}
        or any(
            type(value) is not int or not 0 <= value <= 1000000000
            for value in catalog_counts.values()
        )
    ):
        raise CopyError("invalid_catalog_summary")
    return {
        "file_sha256": summary["file_sha256"],
        "complete": summary["complete"],
        "counts": catalog_counts,
        "records": counts,
        "enumeration_complete": summary.get("enumeration_complete") is True,
        "resolution_complete": summary.get("resolution_complete") is True,
    }


class Checkpoint:
    """An exclusive process lock and FULL-synchronous SQLite transactions."""

    def __init__(
        self, path, *, create=False, on_save=None, max_bytes=None, strict_closure=False
    ):
        self.on_save, self.strict_closure = on_save, strict_closure
        self.path, self.db, self.fd = Path(path).absolute(), None, None
        private_parent(self.path)
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            self.fd = os.open(self.path, flags, 0o600)
            info = os.fstat(self.fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise CopyError("private_checkpoint_required")
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CopyError("checkpoint_busy") from None
            self.db = sqlite3.connect(self.path, timeout=0)
            if attributes(info)[:2] != attributes(self.path.stat())[:2]:
                raise CopyError("checkpoint_changed")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA cache_size=-2048")
            if max_bytes is not None:
                page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
                self.db.execute("PRAGMA max_page_count=%d" % (max_bytes // page_size))
            if create:
                self.db.executescript(
                    "CREATE TABLE metadata(id INTEGER PRIMARY KEY CHECK(id=1),data TEXT,digest TEXT);"
                    "CREATE TABLE results(id TEXT PRIMARY KEY,data TEXT,digest TEXT);"
                    "CREATE TABLE facts(id INTEGER PRIMARY KEY,data TEXT,digest TEXT);"
                )
            else:
                if self.db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise CopyError("checkpoint_corrupt")
                self.meta = self.read_meta()
                self.evidence_digest()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def decode(data, checksum):
        try:
            value = json.loads(data)
            if not isinstance(value, dict) or digest(value) != checksum:
                raise CopyError("checkpoint_corrupt")
            return value
        except (ValueError, TypeError, ReferenceError):
            raise CopyError("checkpoint_corrupt") from None

    def read_meta(self):
        row = self.db.execute("SELECT data,digest FROM metadata WHERE id=1").fetchone()
        if row is None:
            raise CopyError("checkpoint_corrupt")
        value = self.decode(*row)
        if value.get("schema") not in {SCHEMA, GENERATION_SCHEMA}:
            raise CopyError("checkpoint_schema")
        try:
            if str(uuid.UUID(value["plan_uuid"])) != value["plan_uuid"]:
                raise ValueError
            if value["schema"] == GENERATION_SCHEMA:
                ancestors = value["ancestor_plan_uuids"]
                if (
                    type(value["generation"]) is not int
                    or value["generation"] < 2
                    or not isinstance(ancestors, list)
                    or len(ancestors) != value["generation"] - 1
                    or len(set(ancestors)) != len(ancestors)
                    or value["plan_uuid"] in ancestors
                    or ancestors[-1] != value["parent_plan_uuid"]
                    or any(str(uuid.UUID(item)) != item for item in ancestors)
                    or len(bytes.fromhex(value["parent_evidence_sha256"])) != 32
                    or set(value["generation_summary"]) != set(GENERATION_COUNTS)
                    or any(
                        type(n) is not int or n < 0
                        for n in value["generation_summary"].values()
                    )
                ):
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CopyError("checkpoint_corrupt") from None
        return value

    def validate_row(self, row, *, historical=False):
        try:
            creator = row.get("creation_plan_uuid", self.meta["plan_uuid"])
            allowed = self.meta.get("ancestor_plan_uuids", []) + (
                [] if historical else [self.meta["plan_uuid"]]
            )
            if (
                row["id"] != digest(row["source"])
                or row["source"]["endpoint_url"] != self.meta["source_endpoint"]
                or row["source"]["bucket"] not in self.meta["source_buckets"]
                or creator not in allowed
                or (
                    row.get("is_latest") is not None
                    and type(row["is_latest"]) is not bool
                )
            ):
                raise ValueError
            if row.get("target_key") is not None:
                expected = f"{self.meta['target']['key_prefix']}{creator}/{row['id']}/{row['sha256']}"
                if row["target_key"] != expected or row["source_kind"] != "object":
                    raise ValueError
            if row["decision"] == "copy" and row.get("target_key") is None:
                raise ValueError
            if row.get("receipt") and (
                not physical_intent(row)
                or not isinstance(row["receipt"].get("version_id"), str)
                or row["receipt"]["version_id"] in {"", "null"}
                or not row.get("nonce")
                or not isinstance(row.get("content"), dict)
            ):
                raise ValueError
            if creator != self.meta["plan_uuid"] and not historical:
                record = {"record_type": "result", "value": row}
                proof = self.db.execute(
                    "SELECT data,digest FROM history WHERE id=?", (history_id(record),)
                ).fetchone()
                if proof is None:
                    raise ValueError
                prior = self.decode(*proof)["value"]
                if any(
                    prior.get(k) != row.get(k)
                    for k in ("creation_plan_uuid", "target_key", "nonce", "content")
                ):
                    raise ValueError
                if prior.get("receipt") and prior["receipt"]["version_id"] != (
                    row.get("receipt") or {}
                ).get("version_id"):
                    raise ValueError
        except (KeyError, TypeError, ValueError, AttributeError):
            raise CopyError("checkpoint_corrupt") from None

    def history(self):
        if self.meta["schema"] == SCHEMA:
            return
        for record_id, data, checksum in self.db.execute(
            "SELECT id,data,digest FROM history ORDER BY id"
        ):
            record = self.decode(data, checksum)
            try:
                if (
                    record["record_type"] not in {"result", "fact"}
                    or history_id(record) != record_id
                ):
                    raise ValueError
                if record["record_type"] == "result":
                    self.validate_row(record["value"], historical=True)
                elif record["value"]["type"] not in {"bucket", "multipart", "issue"}:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise CopyError("checkpoint_corrupt") from None
            yield record

    def evidence_digest(self):
        """Validated logical evidence, independent of SQLite pages and row order."""
        self.meta = self.read_meta()
        sha = hashlib.sha256()
        sha.update(canonical({"record_type": "metadata", "value": self.meta}))
        history_count = 0
        for record in self.history():
            sha.update(canonical({"record_type": "history", "value": record}))
            history_count += 1
        count = 0
        for row in self.rows():
            sha.update(canonical({"record_type": "current", "value": row}))
            count += 1
        fact_counts = {}
        # Sort logical JSON, never local insertion IDs. Canonical serialization
        # is already required by writers, but decoding still verifies each fact.
        for data, checksum in self.db.execute(
            "SELECT data,digest FROM facts ORDER BY digest,data"
        ):
            fact = self.decode(data, checksum)
            kind = fact.get("type")
            if kind not in {"bucket", "multipart", "issue"}:
                raise CopyError("checkpoint_corrupt")
            fact_counts[kind] = fact_counts.get(kind, 0) + 1
            sha.update(canonical({"record_type": "fact", "value": fact}))
        if (
            count != self.meta["result_count"]
            or fact_counts != self.meta["fact_counts"]
            or history_count != self.meta.get("history_count", 0)
            or (
                self.meta["schema"] == GENERATION_SCHEMA
                and history_count != self.meta["generation_summary"]["history_records"]
            )
        ):
            raise CopyError("checkpoint_corrupt")
        return sha.hexdigest()

    def rows(self):
        for object_id, data, checksum in self.db.execute(
            "SELECT id,data,digest FROM results ORDER BY id"
        ):
            value = self.decode(data, checksum)
            if value.get("id") != object_id:
                raise CopyError("checkpoint_corrupt")
            self.validate_row(value)
            yield value

    def save(self, row):
        self.validate_row(row)
        if self.on_save is not None:
            # Durable remote intent/ACK comes first. Local disk failure must not
            # erase an already-observed provider receipt or permit another I/O.
            self.on_save(json.loads(packed(row)), digest(row))
        try:
            self.db.execute(
                "UPDATE results SET data=?,digest=? WHERE id=?",
                (packed(row), digest(row), row["id"]),
            )
            self.db.commit()
        except Exception:
            if self.on_save is not None:
                raise CopyStopped("copy_local_checkpoint_failed") from None
            raise

    def status(self):
        counts, decisions = {}, {}
        for row in self.rows():
            counts[row["state"]] = counts.get(row["state"], 0) + 1
            decisions[row["decision"]] = decisions.get(row["decision"], 0) + 1
        result = {
            "schema": self.meta["schema"],
            "counts": counts,
            "decisions": decisions,
            "source": self.meta["source_summary"],
            "catalog": self.meta["catalog_summary"],
            "facts": self.meta["fact_counts"],
            "copy_subset_complete": not any(
                counts.get(k, 0) for k in ("pending", "writing", "uncertain", "failed")
            ),
            "migration_ready": False,
        }
        if self.meta["schema"] == GENERATION_SCHEMA:
            result.update(
                {
                    key: self.meta[key]
                    for key in (
                        "generation",
                        "parent_evidence_sha256",
                        "generation_summary",
                    )
                }
            )
        return result

    def close(self):
        try:
            try:
                if self.db is not None:
                    self.db.close()
            finally:
                if self.fd is not None:
                    os.close(self.fd)
        except BaseException:
            if self.strict_closure:
                error = CopyStopped("storage_operation_unsettled")
                error.storage_operation_unsettled = True
                raise error from None
            raise
        finally:
            self.fd = self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def plan(
    checkpoint,
    source,
    catalog,
    target,
    *,
    handling=None,
    limits=None,
    checkpoint_hook=lambda: None,
    max_bytes=None,
    max_rows=None,
    strict_closure=False,
):
    """Bind both sealed inputs without reopening either after validation."""
    import tempfile

    limits = limits or Limits()
    handling = handling or {}
    if (
        not isinstance(handling, dict)
        or not set(handling) <= PURPOSES
        or any(v not in {"migrate", "rebuild", "retain"} for v in handling.values())
    ):
        raise CopyError("invalid_handling")
    private_parent(Path(checkpoint).absolute())
    with tempfile.TemporaryDirectory(
        prefix="aap-copy-plan-", dir=Path(checkpoint).parent
    ) as directory:
        budget = ReferenceBudget(
            ReferenceLimits(max_seconds=limits.max_seconds),
            checkpoint_hook,
            strict_closure=strict_closure,
        )
        index = PlanSource(Path(directory) / "source.sqlite", source, budget)
        try:
            catalog_summary = read_catalog(catalog, index, budget)
            source_header = index.original_header
            source_endpoint = index.header["endpoint"]
            if (
                endpoint(target["endpoint_url"]) == endpoint(source_endpoint)
                and target["bucket"] in index.buckets
            ):
                raise CopyError("target_is_source")
            selections = source_header.get("handling", {}) | handling
            choices = {}
            for purpose, bucket in source_header["purpose_buckets"].items():
                choices.setdefault(bucket, set()).add(
                    selections.get(purpose, "unreviewed")
                )
            bucket_handling = {
                bucket: next(iter(values))
                if len(values) == 1 and "unreviewed" not in values
                else "ambiguous"
                for bucket, values in choices.items()
            }
            plan_uuid = str(uuid.uuid4())
            with Checkpoint(
                checkpoint,
                create=True,
                max_bytes=max_bytes,
                strict_closure=strict_closure,
            ) as state:
                facts = {}
                for (data,) in index.db.execute("SELECT data FROM facts ORDER BY id"):
                    row = json.loads(data)
                    facts[row["type"]] = facts.get(row["type"], 0) + 1
                    state.db.execute(
                        "INSERT INTO facts(data,digest) VALUES(?,?)",
                        (data, digest(row)),
                    )
                count = matched = 0
                source_counts = dict.fromkeys(
                    (
                        "objects",
                        "delete_markers",
                        "verified_objects",
                        "unverified_objects",
                        "over_object_limit",
                        "mutable_null_objects",
                        "unpinned_objects",
                    ),
                    0,
                )
                for bucket, key, version_json, kind, data, match in index.db.execute(
                    "SELECT bucket,key,version,kind,data,matched FROM objects ORDER BY bucket,key,version,kind"
                ):
                    budget.check()
                    if max_rows is not None and count >= max_rows:
                        raise CopyError("plan_row_budget")
                    version, info = json.loads(version_json), json.loads(data)
                    source_counts[
                        "objects" if kind == "object" else "delete_markers"
                    ] += 1
                    if kind == "object":
                        source_counts[
                            "verified_objects"
                            if info["verification"] == "verified"
                            else "unverified_objects"
                        ] += 1
                        source_counts["over_object_limit"] += int(
                            info["size_bytes"] > limits.max_object_bytes
                        )
                        source_counts["mutable_null_objects"] += int(version == "null")
                        source_counts["unpinned_objects"] += int(version is None)
                    locator = {
                        "endpoint_url": source_endpoint,
                        "bucket": bucket,
                        "key": key,
                        "version_id": version,
                    }
                    object_id = digest(locator)
                    selection = bucket_handling[bucket]
                    decision = (
                        "delete_marker"
                        if kind == "delete_marker"
                        else "handling_ambiguous"
                        if selection == "ambiguous"
                        else selection
                        if selection != "migrate"
                        else "source_unverified"
                        if info["verification"] != "verified"
                        else "object_byte_budget"
                        if info["size_bytes"] > limits.max_object_bytes
                        else "copy"
                    )
                    row = {
                        "id": object_id,
                        "source": locator,
                        **info,
                        "source_kind": kind,
                        "source_exact_catalog_match": bool(match),
                        "decision": decision,
                        "state": "pending" if decision == "copy" else "skipped",
                        "target_key": f"{target['key_prefix']}{plan_uuid}/{object_id}/{info['sha256']}"
                        if decision == "copy"
                        else None,
                        "nonce": None,
                        "receipt": None,
                        "error": None,
                    }
                    state.db.execute(
                        "INSERT INTO results VALUES(?,?,?)",
                        (object_id, packed(row), digest(row)),
                    )
                    count += 1
                    matched += int(bool(match))
                state.meta = {
                    "schema": SCHEMA,
                    "plan_uuid": plan_uuid,
                    "created_at": now(),
                    "source_endpoint": source_endpoint,
                    "source_buckets": sorted(index.buckets),
                    "source_sha256": index.summary["file_sha256"],
                    "catalog_sha256": catalog_summary["file_sha256"],
                    "source_summary": {
                        "complete": index.summary["complete"],
                        "observed_versions_and_markers": count,
                        **source_counts,
                        "exact_catalog_matches": matched,
                        "without_exact_match_meaning": "not matched in this bounded catalog; never orphan",
                    },
                    "catalog_summary": catalog_summary,
                    "target": target_identity(target),
                    "handling": selections,
                    "bucket_handling": bucket_handling,
                    "limits": asdict(limits),
                    "result_count": count,
                    "fact_counts": facts,
                    "migration_ready": False,
                }
                state.db.execute(
                    "INSERT INTO metadata VALUES(1,?,?)",
                    (packed(state.meta), digest(state.meta)),
                )
                state.db.commit()
                return state.status()
        finally:
            index.close()


def plan_generation(
    checkpoint_path,
    source_report,
    catalog_path,
    target,
    *,
    parent,
    parent_evidence_sha256,
    handling=None,
    limits=None,
    checkpoint_hook=None,
    max_bytes=None,
    max_rows=None,
    strict_closure=False,
):
    """Create a self-contained child without changing any parent evidence."""
    checkpoint_hook = checkpoint_hook or (lambda: None)
    if parent.evidence_digest() != parent_evidence_sha256:
        raise CopyError("parent_evidence_mismatch")
    if target_identity(target) != parent.meta["target"]:
        raise CopyError("generation_target_mismatch")
    limits = limits or Limits(**parent.meta["limits"])
    handling = parent.meta["handling"] if handling is None else handling
    if asdict(limits) != parent.meta["limits"] or handling != parent.meta["handling"]:
        raise CopyError("generation_policy_mismatch")
    # The ordinary sealed-input planner remains the only input projection path.
    plan(
        checkpoint_path,
        source_report,
        catalog_path,
        target,
        handling=handling,
        limits=limits,
        checkpoint_hook=checkpoint_hook,
        max_bytes=max_bytes,
        max_rows=max_rows,
        strict_closure=strict_closure,
    )
    with Checkpoint(
        checkpoint_path, max_bytes=max_bytes, strict_closure=strict_closure
    ) as state:
        if any(
            state.meta[k] != parent.meta[k]
            for k in ("source_endpoint", "source_buckets", "bucket_handling")
        ):
            raise CopyError("generation_source_scope_mismatch")
        budget = Budget(limits)

        def check():
            checkpoint_hook()
            budget.check()

        summary = dict.fromkeys(GENERATION_COUNTS, 0)
        state.db.execute(
            "CREATE TABLE history(id TEXT PRIMARY KEY,data TEXT,digest TEXT)"
        )
        state.db.execute("CREATE TEMP TABLE prior(source_id TEXT,data TEXT)")
        state.db.execute("CREATE INDEX prior_source ON prior(source_id)")
        history_count = 0
        row_limit = (
            max_rows if max_rows is not None else ReferenceLimits().max_source_entries
        )

        def remember(record):
            nonlocal history_count
            check()
            record_id = history_id(record)
            previous = state.db.execute(
                "SELECT data,digest FROM history WHERE id=?", (record_id,)
            ).fetchone()
            if previous:
                old = state.decode(*previous)
                if record["record_type"] == "result" and physical_intent(
                    record["value"]
                ):
                    before, after = old["value"], record["value"]
                    if any(
                        before.get(k) != after.get(k)
                        for k in ("creation_plan_uuid", "nonce", "content")
                    ):
                        raise CopyError("history_identity_conflict")
                    if before.get("receipt"):
                        if (
                            after.get("receipt")
                            and before["receipt"]["version_id"]
                            != after["receipt"]["version_id"]
                        ):
                            raise CopyError("history_receipt_conflict")
                        if not after.get("receipt"):
                            record = old
                else:
                    # Keep the first creation attribution for non-physical facts.
                    record = old
            else:
                if state.meta["result_count"] + history_count >= row_limit:
                    raise CopyError("plan_row_budget")
                history_count += 1
            state.db.execute(
                "INSERT OR REPLACE INTO history VALUES(?,?,?)",
                (record_id, packed(record), digest(record)),
            )

        for record in parent.history():
            remember(record)
        for row in parent.rows():
            row = {
                **row,
                "creation_plan_uuid": row.get(
                    "creation_plan_uuid", parent.meta["plan_uuid"]
                ),
                "is_latest": row.get("is_latest"),
            }
            remember({"record_type": "result", "value": row})
        for data, checksum in parent.db.execute(
            "SELECT data,digest FROM facts ORDER BY digest,data"
        ):
            remember({"record_type": "fact", "value": parent.decode(data, checksum)})
        # A bounded disk-backed join also finds physical versions that disappeared
        # in one intermediate report and reappear in a later generation.
        for (data,) in state.db.execute(
            "SELECT data FROM history WHERE id LIKE 'r:%' ORDER BY id"
        ):
            check()
            row = json.loads(data)["value"]
            state.db.execute("INSERT INTO prior VALUES(?,?)", (row["id"], packed(row)))
        for row in state.rows():
            check()
            row["creation_plan_uuid"] = state.meta["plan_uuid"]
            prior = [
                json.loads(data)
                for (data,) in state.db.execute(
                    "SELECT data FROM prior WHERE source_id=?", (row["id"],)
                )
            ]
            if row["source_kind"] == "delete_marker":
                summary["delete_markers"] += 1
            elif row["source"]["version_id"] not in (None, "null"):
                objects = [old for old in prior if old["source_kind"] == "object"]
                if any(
                    any(
                        old.get(k) != row.get(k)
                        for k in ("sha256", "size_bytes", "etag")
                    )
                    for old in objects
                ):
                    row.update(
                        decision="source_identity_conflict",
                        state="skipped",
                        target_key=None,
                    )
                    summary["source_identity_conflicts"] += 1
                elif row["decision"] == "copy":
                    reusable = [old for old in objects if physical_intent(old)]
                    if reusable:
                        # Receipts are stronger than unknown-ACK intents. Preserve
                        # the oldest stable mapping when several facts exist.
                        old = sorted(
                            reusable,
                            key=lambda item: (
                                not bool(item.get("receipt")),
                                item["target_key"],
                            ),
                        )[0]
                        for key in (
                            "creation_plan_uuid",
                            "target_key",
                            "nonce",
                            "content",
                            "receipt",
                        ):
                            if key in old:
                                row[key] = old[key]
                        summary["reused_objects"] += 1
                    else:
                        summary["new_objects"] += 1
            elif row["decision"] == "copy":
                summary[
                    "mutable_recopies"
                    if any(old["source_kind"] == "object" for old in prior)
                    else "new_objects"
                ] += 1
            state.db.execute(
                "UPDATE results SET data=?,digest=? WHERE id=?",
                (packed(row), digest(row), row["id"]),
            )
        for row in parent.rows():
            check()
            if (
                row["source_kind"] == "object"
                and state.db.execute(
                    "SELECT 1 FROM results WHERE id=?", (row["id"],)
                ).fetchone()
                is None
            ):
                summary["previous_only_objects"] += 1
        summary["history_records"] = history_count
        state.meta.update(
            schema=GENERATION_SCHEMA,
            generation=parent.meta.get("generation", 1) + 1,
            parent_plan_uuid=parent.meta["plan_uuid"],
            parent_evidence_sha256=parent_evidence_sha256,
            ancestor_plan_uuids=parent.meta.get("ancestor_plan_uuids", [])
            + [parent.meta["plan_uuid"]],
            history_count=history_count,
            generation_summary=summary,
        )
        state.db.execute(
            "UPDATE metadata SET data=?,digest=? WHERE id=1",
            (packed(state.meta), digest(state.meta)),
        )
        state.db.execute("DROP TABLE prior")
        state.db.commit()
        state.evidence_digest()
        return state.status()


def content(response):
    """Preserve actual conditional-read metadata; never the redacted inventory."""
    value = {}
    for name in CONTENT_FIELDS:
        if name not in response:
            continue
        item = response[name]
        if name == "Expires":
            item = item.isoformat() if hasattr(item, "isoformat") else item
        if name == "Metadata":
            if (
                not isinstance(item, dict)
                or any(
                    not isinstance(k, str) or not isinstance(v, str)
                    for k, v in item.items()
                )
                or RESERVED & {k.lower() for k in item}
            ):
                raise CopyError("unsupported_source_metadata")
        elif not isinstance(item, str):
            raise CopyError("unsupported_source_metadata")
        value[name] = item
    value.setdefault("Metadata", {})
    return value


def checked(response):
    if (
        response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 200
        or response.get("DeleteMarker")
        or "ContentRange" in response
    ):
        raise CopyError("unexpected_response")
    return response


def source_headers(response, row):
    checked(response)
    version = row["source"]["version_id"]
    actual = response.get("VersionId")
    if (
        type(response.get("ContentLength")) is not int
        or response["ContentLength"] != row["size_bytes"]
        or response.get("ETag") != row["etag"]
        or (version not in (None, "null") and actual != version)
        or (version in (None, "null") and actual not in (None, "null"))
    ):
        raise CopyError("source_changed")
    return {
        "content": content(response),
        "etag": response["ETag"],
        "version_id": actual,
        "last_modified": str(response.get("LastModified")),
    }


def stream_body(response, row, budget, category, destination=None):
    body = response.get("Body")
    failure = None
    total, sha = 0, hashlib.sha256()
    try:
        if not callable(getattr(body, "read", None)) or not callable(
            getattr(body, "close", None)
        ):
            raise CopyError("invalid_body")
        while True:
            budget.check()
            requested = min(CHUNK, row["size_bytes"] - total + 1)
            data = body.read(requested)
            if not isinstance(data, bytes) or len(data) > requested:
                raise CopyError("invalid_body")
            budget.take(category, len(data))
            total += len(data)
            if total > row["size_bytes"]:
                raise CopyError("body_size_mismatch")
            if not data:
                break
            sha.update(data)
            if destination is not None and destination.write(data) != len(data):
                raise CopyError("disk_write_failed")
        if total != row["size_bytes"] or sha.hexdigest() != row["sha256"]:
            raise CopyError("body_digest_mismatch")
    except BaseException as exc:
        failure = exc
    finally:
        if callable(getattr(body, "close", None)):
            try:
                _close_body(body)
            except Exception:
                failure = CopyError("body_close_failed")
    if failure is not None:
        raise failure
    return sha.hexdigest()


def source_read(client, row, stream, budget):
    budget.reserve("source_bytes", row["size_bytes"] + 1)
    if row["size_bytes"] > budget.limits.max_object_bytes:
        raise CopyError("object_byte_budget")
    args = {
        "Bucket": row["source"]["bucket"],
        "Key": row["source"]["key"],
        "IfMatch": row["etag"],
    }
    if row["source"]["version_id"] is not None:
        args["VersionId"] = row["source"]["version_id"]
    before = source_headers(client.head_object(**args), row)
    response = client.get_object(**args)
    try:
        current = source_headers(response, row)
        if current != before:
            raise CopyError("source_metadata_changed")
    except BaseException:
        _close_body(response["Body"])
        raise
    stream_body(response, row, budget, "source_bytes", stream)
    after = source_headers(client.head_object(**args), row)
    if before != after:
        raise CopyError("source_changed")
    stream.flush()
    os.fsync(stream.fileno())
    stream.seek(0)
    return before["content"]


def missing(exc):
    return str(getattr(exc, "response", {}).get("Error", {}).get("Code")) in {
        "404",
        "NoSuchKey",
        "NoSuchVersion",
        "NotFound",
    }


def require_empty_history(client, state, row):
    listed = client.list_object_versions(
        Bucket=state.meta["target"]["bucket"], Prefix=row["target_key"], MaxKeys=2
    )
    if listed.get("IsTruncated") or any(
        item["Key"] == row["target_key"]
        for kind in ("Versions", "DeleteMarkers")
        for item in listed.get(kind, [])
    ):
        raise CopyError("target_history_conflict")


def creation_metadata(state, row):
    return {
        **row["content"].get("Metadata", {}),
        "aap-copy-plan": row.get("creation_plan_uuid", state.meta["plan_uuid"]),
        "aap-copy-object": row["id"],
        "aap-copy-nonce": row["nonce"],
    }


def target_verify(client, state, row, budget, *, version=None):
    budget.reserve("verify_bytes", row["size_bytes"] + 1)
    args = {"Bucket": state.meta["target"]["bucket"], "Key": row["target_key"]}
    if version is not None:
        args["VersionId"] = version
    head = checked(client.head_object(**args))
    actual = head.get("VersionId")
    if (
        not isinstance(actual, str)
        or not actual
        or actual == "null"
        or (version is not None and actual != version)
    ):
        raise CopyError("target_version_unconfirmed")
    expected = {**row["content"], "Metadata": creation_metadata(state, row)}
    if (
        head.get("ContentLength") != row["size_bytes"]
        or content_target(head) != expected
    ):
        raise CopyError("target_creation_conflict")
    # Pin the observed immutable version even when recovering an unknown PUT ack.
    args["VersionId"] = actual
    response = client.get_object(**args)
    try:
        checked(response)
        if (
            response.get("VersionId") != actual
            or response.get("ContentLength") != row["size_bytes"]
            or response.get("ETag") != head.get("ETag")
            or content_target(response) != expected
        ):
            raise CopyError("target_response_mismatch")
    except BaseException:
        _close_body(response["Body"])
        raise
    stream_body(response, row, budget, "verify_bytes")
    after = checked(client.head_object(**args))
    if (
        after.get("VersionId") != actual
        or after.get("ETag") != head.get("ETag")
        or after.get("ContentLength") != row["size_bytes"]
        or content_target(after) != expected
    ):
        raise CopyError("target_changed")
    return {
        "version_id": actual,
        "sha256": row["sha256"],
        "size_bytes": row["size_bytes"],
        "verified_at": now(),
        "etag": head.get("ETag"),
        "content_sha256": digest(expected),
    }


def content_target(response):
    # Reserved creation metadata is valid only on our target.
    metadata = response.get("Metadata", {})
    clean = {
        **response,
        "Metadata": {k: v for k, v in metadata.items() if k.lower() not in RESERVED},
    }
    result = content(clean)
    result["Metadata"] = metadata
    return result


class UploadBody:
    """Bound SDK reads and count actual submitted body bytes, including retries."""

    def __init__(self, stream, budget):
        self.stream, self.budget = stream, budget

    def read(self, size=-1):
        self.budget.check()
        data = self.stream.read(CHUNK if size < 0 else min(size, CHUNK))
        self.budget.take("upload_bytes", len(data))
        return data

    def seek(self, *args):
        return self.stream.seek(*args)

    def tell(self):
        return self.stream.tell()


@contextmanager
def staging(state):
    """Only one exact plan-owned pathname can survive a hard kill."""
    directory = state.path.parent / (".aap-copy-" + state.meta["plan_uuid"])
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise CopyError("unsafe_staging_directory")
    path = directory / "body.part"
    safe = False
    try:
        # Only this exact file, in this checkpoint's verified private directory.
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise CopyError("unsafe_staging_file")
            path.unlink()
        safe = True
        yield path
    finally:
        if safe and path.exists():
            info = path.lstat()
            if (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
            ):
                path.unlink()
        try:
            directory.rmdir()
        except OSError:
            pass


def _close_body(body):
    try:
        body.close()
    except BaseException:
        if STRICT_CLOSE.get():
            error = CopyStopped("storage_operation_unsettled")
            error.storage_operation_unsettled = True
            raise error from None
        raise


@contextmanager
def staged_body(path, anonymous, strict):
    stream = (
        tempfile.TemporaryFile(mode="w+b", dir=path.parent)
        if anonymous
        else os.fdopen(
            os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600),
            "w+b",
        )
    )
    token = STRICT_CLOSE.set(strict)
    try:
        yield stream
    finally:
        try:
            _close_body(stream)
        finally:
            STRICT_CLOSE.reset(token)


def validate_target(state, config):
    if target_identity(config) != state.meta["target"]:
        raise CopyError("target_identity_mismatch")


def _execute(
    state,
    source_client,
    target_client,
    *,
    limits=None,
    retry_failed=False,
    retry_object_ids=None,
    verify_only=False,
    stopped=lambda: False,
    event=lambda *args: None,
    anonymous_staging=False,
    strict_closure=False,
):
    """Resume uncertain writes by saved creation identity, never by another PUT."""
    limits = limits or Limits(**state.meta["limits"])
    budget = Budget(limits, stopped)
    config = target_client.get_bucket_versioning(Bucket=state.meta["target"]["bucket"])
    if config.get("Status") != "Enabled":
        raise CopyError("target_versioning_required")
    paused = None
    with staging(state) as temp_path:
        for row in state.rows():
            if retry_object_ids is not None and row["id"] not in retry_object_ids:
                continue
            if row["decision"] != "copy":
                continue
            if verify_only:
                if row.get("receipt") is None:
                    continue
            elif row["state"] == "verified" or (
                row["state"] == "failed" and not retry_failed
            ):
                continue
            writing = row["state"] in {"writing", "uncertain"}
            try:
                budget.take("objects", 1)
                event("before_item", row)
                if verify_only or row.get("receipt"):
                    receipt = target_verify(
                        target_client,
                        state,
                        row,
                        budget,
                        version=row["receipt"]["version_id"],
                    )
                else:
                    if row.get("nonce"):
                        try:
                            receipt = target_verify(target_client, state, row, budget)
                        except Exception as exc:
                            if not missing(exc):
                                raise
                            # An unknown write cannot be retried into a new version:
                            # a delete marker could hide its version. Require an empty
                            # exact version history before another conditional PUT.
                            require_empty_history(target_client, state, row)
                        else:
                            row.update(state="verified", receipt=receipt, error=None)
                            state.save(row)
                            event("after_item", row)
                            continue
                    if not row.get("nonce"):
                        require_empty_history(target_client, state, row)
                    budget.reserve("upload_bytes", row["size_bytes"])
                    budget.reserve("verify_bytes", row["size_bytes"] + 1)
                    with staged_body(
                        temp_path, anonymous_staging, strict_closure
                    ) as stream:
                        row["content"] = source_read(source_client, row, stream, budget)
                        event("before_intent", row)
                        row.update(
                            state="writing",
                            nonce=row.get("nonce") or secrets.token_hex(24),
                            error=None,
                        )
                        state.save(row)
                        writing = True
                        event("before_put", row)
                        try:
                            response = target_client.put_object(
                                Bucket=state.meta["target"]["bucket"],
                                Key=row["target_key"],
                                Body=UploadBody(stream, budget),
                                ContentLength=row["size_bytes"],
                                IfNoneMatch="*",
                                **{
                                    **row["content"],
                                    "Metadata": creation_metadata(state, row),
                                },
                            )
                        except BaseException as exc:
                            if str(
                                getattr(exc, "response", {})
                                .get("Error", {})
                                .get("Code")
                            ) in {"412", "PreconditionFailed"}:
                                raise CopyError("target_creation_conflict") from None
                            row.update(state="uncertain", error="put_outcome_unknown")
                            state.save(row)
                            raise
                        event("after_put", row)
                        version = response.get("VersionId")
                        if not version or version == "null":
                            raise CopyError("target_version_unconfirmed")
                        # Save the acknowledged version even when later readback fails.
                        row["receipt"] = {
                            "version_id": version,
                            "put_acknowledged": True,
                        }
                        state.save(row)
                        receipt = target_verify(
                            target_client, state, row, budget, version=version
                        )
                    if not anonymous_staging:
                        temp_path.unlink()
                row.update(state="verified", receipt=receipt, error=None)
                event("before_verified_commit", row)
                state.save(row)
                event("after_item", row)
            except Paused as exc:
                if verify_only:
                    row.update(state="failed", error=exc.code)
                    state.save(row)
                elif writing and row["state"] != "verified":
                    row.update(state="uncertain", error=exc.code)
                    state.save(row)
                paused = exc.code
                break
            except Exception as exc:
                # Keep the original receipt for failed readbacks; verified is never stale.
                row.update(
                    state="uncertain"
                    if writing
                    and row.get("receipt") is None
                    and error_code(exc)
                    not in {"target_creation_conflict", "target_history_conflict"}
                    else "failed",
                    error=error_code(exc),
                )
                state.save(row)
            finally:
                if temp_path.exists():
                    if not anonymous_staging:
                        temp_path.unlink()
    selected = [
        row
        for row in state.rows()
        if retry_object_ids is not None and row["id"] in retry_object_ids
    ]
    return {
        **state.status(),
        "attempt_subset_complete": all(row["state"] == "verified" for row in selected)
        if retry_object_ids is not None
        else None,
        "selected_count": len(retry_object_ids)
        if retry_object_ids is not None
        else None,
        "selected_verified_count": sum(row["state"] == "verified" for row in selected)
        if retry_object_ids is not None
        else None,
        "paused": paused,
        "io": {
            name: getattr(budget, name)
            for name in ("source_bytes", "upload_bytes", "verify_bytes", "objects")
        },
    }


def execute(*args, strict_closure=False, **kwargs):
    token = STRICT_CLOSE.set(strict_closure)
    try:
        return _execute(*args, strict_closure=strict_closure, **kwargs)
    finally:
        STRICT_CLOSE.reset(token)
