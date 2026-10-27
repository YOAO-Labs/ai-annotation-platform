"""Project current full-verified copy rows into immutable physical read routes."""

import hashlib
import tempfile

from app.services import storage_migration_copy as core
from app.services.storage_migration_finalization_evidence import (
    FinalizationEvidenceError,
    build_final_evidence,
)

SCHEMA = "aap.storage-migration-read-mapping/v1"


def mapping_digest(entries):
    """Hash framed canonical entries without treating a whole map as one record."""
    checksum = hashlib.sha256()
    for entry in sorted(entries, key=lambda value: value["source_object_id"]):
        checksum.update(core.canonical(entry))
    return checksum.hexdigest()


def build_read_mapping(values, *, window_id, window_revision):
    """Revalidate full sealed evidence, preserving each original creation receipt.

    Historical-only rows, retained objects, rebuilds and delete markers remain in
    final evidence. Only current verified copy results grant physical read routes.
    This performs no provider I/O and does not establish publication authority.
    """
    evidence = build_final_evidence(
        values, window_id=window_id, window_revision=window_revision
    )
    try:
        from app.services.storage_migration_copies import BASELINE_LIMIT, materialize

        with tempfile.TemporaryDirectory(prefix="aap-read-mapping-") as directory:
            path = materialize(directory, values)
            with core.Checkpoint(
                path, max_bytes=BASELINE_LIMIT, strict_closure=True
            ) as state:
                if state.evidence_digest() != evidence["evidence_sha256"]:
                    raise FinalizationEvidenceError("finalization_checkpoint_binding")
                entries = []
                for row in state.rows():
                    if row["decision"] != "copy":
                        continue
                    source, receipt = row["source"], row["receipt"]
                    entries.append(
                        {
                            "source_object_id": row["id"],
                            "source_endpoint_url": source["endpoint_url"],
                            "source_bucket": source["bucket"],
                            "source_object_key": source["key"],
                            "source_version_id": source["version_id"],
                            "sha256": row["sha256"],
                            "size_bytes": row["size_bytes"],
                            "source_etag": row["etag"],
                            "is_latest": row["is_latest"],
                            "target_object_key": row["target_key"],
                            "target_version_id": receipt["version_id"],
                            "target_etag": receipt["etag"],
                            "target_content_sha256": receipt["content_sha256"],
                            "creation_nonce": row["nonce"],
                            "creation_plan_uuid": row.get(
                                "creation_plan_uuid", state.meta["plan_uuid"]
                            ),
                        }
                    )
        if len(entries) != evidence["counts"]["copied_objects"]:
            raise FinalizationEvidenceError("finalization_checkpoint_binding")
        entries.sort(key=lambda row: row["source_object_id"])
        return {
            "schema": SCHEMA,
            "evidence": evidence,
            "entries": entries,
            "mapping_sha256": mapping_digest(entries),
        }
    except FinalizationEvidenceError:
        raise
    except Exception:
        raise FinalizationEvidenceError("finalization_read_mapping_invalid") from None
