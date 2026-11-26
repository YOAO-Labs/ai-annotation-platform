"""Verify trainer-emitted input membership receipts before publication."""

import json

from training_contract.input_membership import verify_receipt


def verified_local_receipt(config, run_id, attempt_id, path):
    """Keep the existing execution manifest as the only uploaded receipt artifact."""
    try:
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("Training input receipt exceeds the report limit")
        proof = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError("Training input receipt is missing or unreadable") from exc
    verify_receipt(proof, config, run_id, attempt_id)
    return proof
