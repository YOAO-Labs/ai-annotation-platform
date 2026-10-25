"""Bounded local validation of a final inventory and materialized copy evidence.

This projection performs no provider I/O. Its caller separately owns the last
full target verification, current authority, window CAS and live native fence.
"""

from collections import Counter
from dataclasses import asdict
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import uuid

from app.services import storage_migration_copy as core
from app.services.storage_migration_inventory import PURPOSES, verify_inventory
from app.services.storage_migration_references import Budget, Limits

SCHEMA = "aap.storage-migration-final-evidence/v1"
MAX_INTEGER = 2**63 - 1
SHA = re.compile(r"[0-9a-f]{64}\Z")
CATALOG_COUNTS = (
    "references",
    "unexpanded",
    "issues",
    "resolution_unknown",
    "conflicts",
)
COUNT_KEYS = (
    "current_objects",
    "current_delete_markers",
    "copied_objects",
    "retained_objects",
    "rebuild_objects",
    "verified_target_bytes",
    "source_buckets",
    "source_verified_bytes",
    "mutable_null_objects",
    "unpinned_objects",
    "over_object_limit",
    "exact_catalog_matches",
    "previous_only_objects",
    "history_records",
)


class FinalizationEvidenceError(ValueError):
    def __init__(self, code):
        self.code = (
            code
            if isinstance(code, str)
            and re.fullmatch(r"finalization_[a-z][a-z0-9_]{0,63}", code)
            else "finalization_evidence_invalid"
        )
        super().__init__(self.code)


def _require(condition, code="finalization_evidence_invalid"):
    if not condition:
        raise FinalizationEvidenceError(code)


def _uuid(value):
    return isinstance(value, str) and str(uuid.UUID(value)) == value


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _integer(value):
    return type(value) is int and 0 <= value <= MAX_INTEGER


def _catalog(summary):
    return {
        "complete": summary["complete"],
        "enumeration_complete": summary["enumeration_complete"],
        "resolution_complete": summary["resolution_complete"],
        "counts": {key: summary["counts"].get(key, 0) for key in CATALOG_COUNTS},
    }


def _stamp(value):
    _require(
        isinstance(value, str) and len(value) <= 64,
        "finalization_target_receipt_invalid",
    )
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(stamp.utcoffset() is not None, "finalization_target_receipt_invalid")


def _receipt(state, row):
    receipt = row.get("receipt")
    _require(
        row["state"] == "verified"
        and isinstance(receipt, dict)
        and isinstance(row.get("nonce"), str)
        and bool(row["nonce"])
        and isinstance(row.get("content"), dict),
        "finalization_target_receipt_invalid",
    )
    _require(
        isinstance(receipt.get("version_id"), str)
        and receipt["version_id"] not in {"", "null"}
        and receipt.get("sha256") == row["sha256"]
        and type(receipt.get("size_bytes")) is int
        and receipt["size_bytes"] == row["size_bytes"]
        and isinstance(receipt.get("etag"), str)
        and bool(receipt["etag"])
        and core.content(row["content"]) == row["content"],
        "finalization_target_receipt_invalid",
    )
    expected = {**row["content"], "Metadata": core.creation_metadata(state, row)}
    _require(
        receipt.get("content_sha256") == core.digest(expected),
        "finalization_target_receipt_invalid",
    )
    _stamp(receipt.get("verified_at"))


def _source_facts(index, state):
    facts = []
    for (data,) in index.db.execute("SELECT data FROM facts ORDER BY id"):
        fact = json.loads(data)
        _require(
            fact["type"] not in {"issue", "multipart"}, "finalization_source_incomplete"
        )
        _require(len(facts) < len(index.buckets), "finalization_source_incomplete")
        facts.append(fact)
    grouped = {}
    for purpose, bucket in index.original_header["purpose_buckets"].items():
        grouped.setdefault(bucket, []).append(purpose)
    _require(len(facts) == len(grouped), "finalization_source_incomplete")
    seen = set()
    for fact in facts:
        bucket = fact.get("bucket")
        _require(
            fact.get("type") == "bucket"
            and bucket in grouped
            and bucket not in seen
            and sorted(fact.get("purposes", [])) == sorted(grouped[bucket])
            and fact.get("versioning", {}).get("state") == "known"
            and fact["versioning"].get("status") in {"Enabled", "Suspended", "Disabled"}
            and fact.get("lifecycle", {}).get("state") == "known"
            and isinstance(fact["lifecycle"].get("rules"), list),
            "finalization_source_incomplete",
        )
        seen.add(bucket)
    saved = Counter()
    for data, checksum in state.db.execute("SELECT data,digest FROM facts"):
        saved[core.packed(state.decode(data, checksum))] += 1
    _require(
        saved == Counter(core.packed(fact) for fact in facts),
        "finalization_source_binding",
    )


def _rows(index, state, choices, counts, budget):
    source_counts = Counter()
    for row in state.rows():
        budget.check()
        source = row["source"]
        record = index.db.execute(
            "SELECT data,matched FROM objects WHERE bucket=? AND key=? AND version=? AND kind=?",
            (
                source["bucket"],
                source["key"],
                json.dumps(source["version_id"]),
                row["source_kind"],
            ),
        ).fetchone()
        _require(record is not None, "finalization_source_binding")
        info = json.loads(record[0])
        _require(
            all(row.get(key) == value for key, value in info.items())
            and type(row.get("source_exact_catalog_match")) is bool
            and row["source_exact_catalog_match"] == bool(record[1])
            and type(row.get("is_latest")) is bool,
            "finalization_source_binding",
        )
        source_counts["observed_versions_and_markers"] += 1
        counts["exact_catalog_matches"] += int(bool(record[1]))
        decision = row["decision"]
        policy = choices[source["bucket"]]
        if row["source_kind"] == "delete_marker":
            _require(
                decision == "delete_marker"
                and row["state"] == "skipped"
                and isinstance(source["version_id"], str)
                and bool(source["version_id"]),
                "finalization_decision_blocked",
            )
            counts["current_delete_markers"] += 1
        else:
            _require(
                row["source_kind"] == "object"
                and info["verification"] == "verified"
                and _sha(info["sha256"]),
                "finalization_source_incomplete",
            )
            counts["current_objects"] += 1
            counts["source_verified_bytes"] += info["size_bytes"]
            counts["mutable_null_objects"] += int(source["version_id"] == "null")
            counts["unpinned_objects"] += int(source["version_id"] is None)
            counts["over_object_limit"] += int(
                info["size_bytes"] > state.meta["limits"]["max_object_bytes"]
            )
            if decision == "copy":
                _require(
                    policy == "migrate"
                    and info["size_bytes"] <= state.meta["limits"]["max_object_bytes"],
                    "finalization_decision_blocked",
                )
                _receipt(state, row)
                counts["copied_objects"] += 1
                counts["verified_target_bytes"] += info["size_bytes"]
            else:
                _require(
                    decision in {"retain", "rebuild"}
                    and decision == policy
                    and row["state"] == "skipped",
                    "finalization_decision_blocked",
                )
                counts[
                    "retained_objects" if decision == "retain" else "rebuild_objects"
                ] += 1
        if decision != "copy":
            _require(
                all(row.get(key) is None for key in ("target_key", "nonce", "receipt")),
                "finalization_decision_blocked",
            )
    _require(
        source_counts["observed_versions_and_markers"] == index.count,
        "finalization_source_binding",
    )
    return {
        "complete": True,
        "observed_versions_and_markers": index.count,
        "objects": counts["current_objects"],
        "delete_markers": counts["current_delete_markers"],
        "verified_objects": counts["current_objects"],
        "unverified_objects": 0,
        **{
            key: counts[key]
            for key in (
                "over_object_limit",
                "mutable_null_objects",
                "unpinned_objects",
                "exact_catalog_matches",
            )
        },
    }


def _build(values, window_id, window_revision):
    # Import lazily: the domain owner also imports this pure evidence projection.
    from app.db.models.storage_migration_copy import (
        BASELINE_LIMIT,
        CATALOG_LIMIT,
        ROW_LIMIT,
    )
    from app.db.models.storage_migration_inventory import REPORT_LIMIT
    from app.services.storage_migration_copies import materialize

    _require(
        _uuid(str(window_id))
        and type(window_revision) is int
        and 1 <= window_revision <= MAX_INTEGER,
        "finalization_window_binding",
    )
    _require(
        str(values["window_id"]) == str(window_id)
        and type(values["window_revision"]) is int
        and values["window_revision"] == window_revision,
        "finalization_window_binding",
    )
    for name, maximum in (
        ("source_report", REPORT_LIMIT),
        ("catalog", CATALOG_LIMIT),
        ("baseline", BASELINE_LIMIT),
    ):
        raw = values[name]
        _require(
            isinstance(raw, bytes)
            and 0 < len(raw) <= maximum
            and _sha(values[name + "_sha256"])
            and hashlib.sha256(raw).hexdigest() == values[name + "_sha256"],
            "finalization_input_integrity",
        )
    _require(
        type(values["generation"]) is int
        and values["generation"] >= 2
        and _uuid(str(values["parent_copy_id"]))
        and _sha(values["parent_evidence_sha256"]),
        "finalization_generation_binding",
    )
    _require(
        isinstance(values["deltas"], list) and len(values["deltas"]) <= ROW_LIMIT,
        "finalization_input_budget",
    )
    _require(
        sum(len(core.canonical(delta)) for delta in values["deltas"]) <= BASELINE_LIMIT,
        "finalization_input_budget",
    )
    verify_inventory(stream=io.BytesIO(values["source_report"]))
    for line in io.BytesIO(values["source_report"]):
        _require(
            json.loads(line).get("type")
            in {
                "header",
                "bucket",
                "object",
                "delete_marker",
                "multipart",
                "issue",
                "summary",
                "tail",
            },
            "finalization_source_incomplete",
        )
    with tempfile.TemporaryDirectory(prefix="aap-migration-final-") as directory:
        root = Path(directory)
        for name, file in (
            ("source_report", "source.jsonl"),
            ("catalog", "catalog.jsonl"),
        ):
            path = root / file
            path.write_bytes(values[name])
            path.chmod(0o600)
        budget = Budget(
            Limits(
                max_source_bytes=REPORT_LIMIT,
                max_output_bytes=CATALOG_LIMIT,
                max_source_entries=ROW_LIMIT,
            ),
            strict_closure=True,
        )
        index = core.PlanSource(root / "source.sqlite", root / "source.jsonl", budget)
        try:
            catalog = core.read_catalog(root / "catalog.jsonl", index, budget)
            header, tail = index.original_header, index.summary
            window = header.get("observations", {}).get("migration_window")
            _require(
                isinstance(window, dict)
                and type(window.get("window_revision")) is int
                and window.get("source_quiescence_proven") is False
                and window
                == {
                    "window_id": str(window_id),
                    "window_revision": window_revision,
                    "source_quiescence_proven": False,
                },
                "finalization_window_binding",
            )
            _require(
                header["source"]["endpoint"] == values["source"]["endpoint"]
                and header["purpose_buckets"] == values["source"]["purpose_buckets"],
                "finalization_source_binding",
            )
            _require(tail["complete"] is True, "finalization_source_incomplete")
            handling = values["handling"]
            _require(
                isinstance(handling, dict)
                and set(handling) == PURPOSES
                and all(
                    v in {"migrate", "retain", "rebuild"} for v in handling.values()
                ),
                "finalization_handling_invalid",
            )
            choices = {}
            for purpose, bucket in header["purpose_buckets"].items():
                _require(
                    bucket not in choices or choices[bucket] == handling[purpose],
                    "finalization_handling_invalid",
                )
                choices[bucket] = handling[purpose]
            path = materialize(directory, values)
            with core.Checkpoint(
                path, max_bytes=BASELINE_LIMIT, strict_closure=True
            ) as state:
                evidence = state.evidence_digest()
                meta = state.meta
                _require(
                    meta["source_sha256"] == values["source_report_sha256"]
                    and meta["catalog_sha256"] == values["catalog_sha256"]
                    and meta["source_endpoint"] == header["source"]["endpoint"]
                    and meta["source_buckets"] == sorted(index.buckets)
                    and meta["target"] == core.target_identity(values["target"])
                    and meta["handling"] == handling
                    and meta["bucket_handling"] == choices
                    and meta["limits"] == asdict(core.Limits(**values["limits"]))
                    and meta["catalog_summary"] == catalog,
                    "finalization_checkpoint_binding",
                )
                _source_facts(index, state)
                counts = dict.fromkeys(COUNT_KEYS, 0)
                counts["source_buckets"] = len(index.buckets)
                counts["history_records"] = meta.get("history_count", 0)
                counts["previous_only_objects"] = meta.get(
                    "generation_summary", {}
                ).get("previous_only_objects", 0)
                source_summary = _rows(index, state, choices, counts, budget)
                _require(
                    all(
                        meta["source_summary"].get(k) == v
                        for k, v in source_summary.items()
                    ),
                    "finalization_source_binding",
                )
                expected = {
                    "buckets": counts["source_buckets"],
                    "objects": counts["current_objects"],
                    "delete_markers": counts["current_delete_markers"],
                    "multipart": 0,
                    "verified_objects": counts["current_objects"],
                    "verified_bytes": counts["source_verified_bytes"],
                    "bytes_read": counts["source_verified_bytes"],
                    "issues": 0,
                    "entries": index.count,
                }
                actual = tail.get("counts")
                _require(
                    isinstance(actual, dict)
                    and set(actual) == set(expected) | {"pages"}
                    and all(_integer(v) for v in actual.values())
                    and all(actual[k] == v for k, v in expected.items()),
                    "finalization_source_incomplete",
                )
                _require(
                    all(_integer(v) for v in counts.values()),
                    "finalization_input_budget",
                )
                result = {
                    "schema": SCHEMA,
                    "evidence_sha256": evidence,
                    **{
                        key + "_sha256": values[key + "_sha256"]
                        for key in ("source_report", "catalog", "baseline")
                    },
                    "counts": counts,
                    "catalog_summary": _catalog(catalog),
                }
                _require(
                    len(core.canonical(result)) <= 16384, "finalization_input_budget"
                )
                return result
        finally:
            index.close()


def build_final_evidence(values, *, window_id, window_revision):
    try:
        return _build(values, window_id, window_revision)
    except FinalizationEvidenceError:
        raise
    except Exception:
        # Native cancellation remains propagated; private input never enters errors.
        raise FinalizationEvidenceError("finalization_evidence_invalid") from None
