"""Materialize a persisted copy checkpoint with its exact accepted deltas."""

from pathlib import Path

from app.db.models.storage_migration_copy import BASELINE_LIMIT
from app.services.storage_migration_copy import Checkpoint, CopyError, digest


def materialize(directory, values):
    path = Path(directory) / "checkpoint.sqlite"
    path.write_bytes(values["baseline"])
    path.chmod(0o600)
    with Checkpoint(path, max_bytes=BASELINE_LIMIT, strict_closure=True) as state:
        if (
            state.meta.get("generation", 1) != values["generation"]
            or state.meta.get("parent_evidence_sha256")
            != values["parent_evidence_sha256"]
        ):
            raise CopyError("copy_checkpoint_integrity")
        for object_id, data, checksum in values["deltas"]:
            if object_id != data.get("id") or digest(data) != checksum:
                raise CopyError("copy_checkpoint_integrity")
            saved = state.db.execute(
                "SELECT data,digest FROM results WHERE id=?", (object_id,)
            ).fetchone()
            if saved is None:
                raise CopyError("copy_checkpoint_integrity")
            original = state.decode(*saved)
            mutable = {"state", "nonce", "receipt", "error", "content"}
            if {k: v for k, v in data.items() if k not in mutable} != {
                k: v for k, v in original.items() if k not in mutable
            } or (original.get("nonce") and data.get("nonce") != original["nonce"]):
                raise CopyError("copy_checkpoint_integrity")
            state.save(data)
    return path
