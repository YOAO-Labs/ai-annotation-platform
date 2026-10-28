"""Frozen legacy vectors for pool capability canonicalization and diffs."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
    diff_capabilities,
)
from aap_inference_contract.topology import freeze_json

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


def test_managed_batchable_models_are_idempotent_and_semantically_distinct() -> None:
    raw = {
        "models": [{
            "id": "t",
            "task": "detect",
            "resource_profile": {"batchable": True},
            "supported_variants": [{"key": "size", "variants": [{"value": "t"}]}],
        }],
        "managed_model_compatibility": [
            {"framework": "libreyolo", "format": "libreyolo", "runtime_device": "cuda"}
        ],
    }
    canonical = canonicalize_capability(raw)
    assert canonical["models"][0]["batchable"] is True
    assert canonicalize_capability(canonical) == canonical
    assert canonicalize_capability(freeze_json(canonical)) == canonical
    assert capability_fingerprint(raw) == capability_fingerprint(canonical)
    assert capability_fingerprint(freeze_json(raw)) == capability_fingerprint(canonical)
    assert capability_fingerprint(freeze_json(canonical)) == capability_fingerprint(raw)

    nonbatchable = deepcopy(raw)
    nonbatchable["models"][0]["resource_profile"]["batchable"] = False
    incompatible = deepcopy(raw)
    incompatible["managed_model_compatibility"][0]["runtime_device"] = "cpu"
    for changed, field in ((nonbatchable, "models"), (incompatible, "managed_model_compatibility")):
        assert capability_fingerprint(changed) != capability_fingerprint(raw)
        mismatch = diff_capabilities(canonical, canonicalize_capability(changed))
        assert mismatch is not None
        assert mismatch.differing_fields == (field,)
