"""Frozen legacy vectors for pool capability canonicalization and diffs."""

from __future__ import annotations

import json
from pathlib import Path

from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
    diff_capabilities,
)

_VECTORS = Path(__file__).parent / "fixtures" / "pool_capability_legacy.json"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def test_legacy_canonical_json_and_fingerprint_vectors() -> None:
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))
    for case in vectors["cases"]:
        canonical = canonicalize_capability(case["input"])
        assert _canonical_json(canonical) == _canonical_json(case["canonical"]), case[
            "name"
        ]
        assert capability_fingerprint(case["input"]) == case["fingerprint"], case[
            "name"
        ]
        assert capability_fingerprint(canonical) == case["fingerprint"], case["name"]


def test_legacy_capability_diff_vectors() -> None:
    vectors = json.loads(_VECTORS.read_text(encoding="utf-8"))
    for case in vectors["diffs"]:
        result = diff_capabilities(case["pool"], case["candidate"])
        actual = (
            None
            if result is None
            else {
                "pool_fingerprint": result.pool_fingerprint,
                "candidate_fingerprint": result.candidate_fingerprint,
                "differing_fields": list(result.differing_fields),
            }
        )
        assert actual == case["result"], case["name"]
