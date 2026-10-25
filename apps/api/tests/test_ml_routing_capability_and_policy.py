"""v0.23.3 ADR-0050 §C.1 / §C.2 · capability fingerprint + SWRR golden tests.

Pure-functional core of the routing domain (no Redis / DB). The Redis acquire Lua
in ``ledger.py`` mirrors the SWRR algorithm here; both must stay in lockstep.
"""

from __future__ import annotations

import uuid

import pytest


from aap_inference import pool_capability as cap
from app.services.ml_routing import policy as swrr


# ── Capability fingerprint (§C.1) ─────────────────────────────────────────────


def test_fingerprint_stable_across_field_order() -> None:
    """Same capability, JSON keys shuffled → same fingerprint."""
    a = {"protocol_version": "2", "model_ids": ["sam3", "yolo"], "task": "detect"}
    b = {"task": "detect", "model_ids": ["yolo", "sam3"], "protocol_version": "2"}
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


def test_fingerprint_stable_across_list_order() -> None:
    """List members shuffled → same fingerprint (sorted+deduped internally)."""
    a = {"model_ids": ["a", "b", "c"], "supported_prompts": ["point", "bbox", "text"]}
    b = {"model_ids": ["c", "a", "b"], "supported_prompts": ["text", "point", "bbox"]}
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


def test_fingerprint_missing_defaults_equivalent_to_explicit() -> None:
    """Omitted fields vs explicit defaults → same fingerprint."""
    explicit = {
        "protocol_version": "1",
        "model_ids": [],
        "task": None,
        "modality": None,
        "infra": None,
        "model_version": None,
        "weights_version": None,
        "supported_prompts": [],
        "supported_inputs": [],
        "supported_outputs": [],
        "supported_trackers": [],
        "parameter_schema": {},
        "variant_axes": [],
        "stateful": False,
        "batchable": False,
        "warmup": False,
    }
    omitted = {}  # nothing specified
    assert cap.capability_fingerprint(explicit) == cap.capability_fingerprint(omitted)


def test_fingerprint_excludes_runtime_fields() -> None:
    """URL / GPU UUID / VRAM differences must NOT change fingerprint (replicas interchangeable)."""
    a = {
        "model_ids": ["sam3"],
        "url": "http://gpu-a:9999",
        "gpu_resource_id": "node-a/GPU-1",
    }
    b = {
        "model_ids": ["sam3"],
        "url": "http://gpu-b:9999",
        "gpu_resource_id": "node-b/GPU-2",
    }
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


def test_fingerprint_task_mismatch_produces_diff() -> None:
    pool = {"model_ids": ["sam3"], "task": "segment"}
    cand = {"model_ids": ["sam3"], "task": "detect"}
    diff = cap.diff_capabilities(pool, cand)
    assert diff is not None
    assert "task" in diff.differing_fields
    assert diff.pool_fingerprint != diff.candidate_fingerprint


def test_fingerprint_model_ids_mismatch_produces_diff() -> None:
    pool = {"model_ids": ["sam3", "yolo"]}
    cand = {"model_ids": ["sam3"]}
    diff = cap.diff_capabilities(pool, cand)
    assert diff is not None
    assert "model_ids" in diff.differing_fields


def test_fingerprint_param_schema_mismatch_produces_diff() -> None:
    """A parameter-schema change that affects request legality → mismatch."""
    pool = {"parameter_schema": {"box_threshold": {"type": "float", "required": True}}}
    cand = {"parameter_schema": {"box_threshold": {"type": "float", "required": False}}}
    diff = cap.diff_capabilities(pool, cand)
    assert diff is not None
    assert "parameter_schema" in diff.differing_fields


def test_fingerprint_param_schema_display_fields_ignored() -> None:
    """Display-only fields (label/description/order) do NOT affect fingerprint."""
    a = {
        "parameter_schema": {
            "box_threshold": {"type": "float", "label": "Box Threshold"}
        }
    }
    b = {
        "parameter_schema": {
            "box_threshold": {"type": "float", "label": "框阈值", "order": 1}
        }
    }
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


def test_diff_returns_none_on_identical() -> None:
    snap = {
        "model_ids": ["sam3"],
        "task": "segment",
        "supported_trackers": ["sam3_video"],
    }
    assert cap.diff_capabilities(snap, dict(snap)) is None


def test_fingerprint_is_sha256_hex_64() -> None:
    fp = cap.capability_fingerprint({"model_ids": ["sam3"]})
    assert len(fp) == 64
    int(fp, 16)  # must be valid hex


def test_canonical_snapshot_field_order_fixed() -> None:
    """Canonical snapshot must always present the frozen field set (deterministic encoding)."""
    canon = cap.canonicalize_capability({"model_ids": ["sam3"]})
    from aap_inference.pool_capability import _CANONICAL_FIELDS

    assert set(canon.keys()) == set(_CANONICAL_FIELDS)


def test_variant_axes_normalized_and_sorted() -> None:
    """Variant axes: normalized to {key, values, default} and sorted by key."""
    a = {
        "variant_axes": [
            {"key": "sam", "values": ["tiny", "base"]},
            {"key": "dino", "values": ["large", "small"], "default": "small"},
        ]
    }
    b = {
        "variant_axes": [
            {"key": "dino", "default": "small", "values": ["small", "large"]},
            {"key": "sam", "values": ["base", "tiny"]},
        ]
    }
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


def _production_caps(*, model_id: str = "detector", task: str = "detection") -> dict:
    from aap_inference import extract_capabilities

    result = extract_capabilities(
        {
            "protocol_version": "2",
            "model_version": "weights-7",
            "infra": "onnx",
            "models": [
                {
                    "id": model_id,
                    "task": task,
                    "supported_inputs": ["crop", "full_image"],
                    "supported_geometric_outputs": ["polygon", "bbox"],
                    "supported_variants": [
                        {
                            "key": "size",
                            "label": "Size",
                            "variants": [
                                {"value": "s", "label": "Small"},
                                {"value": "m", "label": "Medium"},
                            ],
                        }
                    ],
                    "default_variants": {"size": "s"},
                    "resource_profile": {"device": "gpu", "batchable": True},
                    "params": {
                        "type": "object",
                        "properties": {
                            "threshold": {
                                "type": "number",
                                "default": 0.5,
                                "description": "UI copy",
                            }
                        },
                    },
                }
            ],
        }
    )
    assert result is not None
    return result


def test_production_models_are_fingerprinted_and_order_independent() -> None:
    a = _production_caps()
    b = _production_caps()
    b["models"][0]["supported_inputs"].reverse()
    b["models"][0]["supported_geometric_outputs"].reverse()
    b["models"][0]["supported_variants"][0]["variants"].reverse()
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


@pytest.mark.parametrize(
    ("field", "mutate"),
    [
        ("models", lambda value: value["models"][0].update(id="other")),
        ("models", lambda value: value["models"][0].update(task="classification")),
        (
            "models",
            lambda value: value["models"][0]["params"]["properties"][
                "threshold"
            ].update(default=0.7),
        ),
        (
            "models",
            lambda value: value["models"][0].update(default_variants={"size": "m"}),
        ),
        (
            "models",
            lambda value: value["models"][0]["resource_profile"].update(
                batchable=False
            ),
        ),
        ("protocol_version", lambda value: value.update(protocol_version="3")),
        ("model_version", lambda value: value.update(model_version="weights-8")),
    ],
)
def test_production_routing_contract_changes_fingerprint(field, mutate) -> None:
    pool = _production_caps()
    candidate = _production_caps()
    mutate(candidate)
    diff = cap.diff_capabilities(pool, candidate)
    assert diff is not None
    assert field in diff.differing_fields


def test_production_display_and_runtime_fields_do_not_change_fingerprint() -> None:
    a = _production_caps()
    b = _production_caps()
    b["models"][0]["display_name"] = "展示名称"
    b["models"][0]["supported_variants"][0]["label"] = "尺寸"
    b["models"][0]["params"]["properties"]["threshold"]["description"] = "另一段文案"
    b["models"][0]["classes"] = [{"index": 0, "name": "loaded-only"}]
    b["models"][0]["resource_profile"]["device"] = "cpu"
    assert cap.capability_fingerprint(a) == cap.capability_fingerprint(b)


# ── SWRR (§C.2) ───────────────────────────────────────────────────────────────


def _ids(n: int) -> list[str]:
    """N deterministic, lexicographically-ordered instance UUID hexes."""
    base = uuid.UUID("00000000-0000-0000-0000-000000000000")
    return [str(uuid.UUID(int=base.int + i)) for i in range(n)]


def test_swrr_weight_1_1_split_50_50() -> None:
    ids = _ids(2)
    cands = [(ids[0], 1), (ids[1], 1)]
    state = swrr.SWRRState()
    counts = {i: 0 for i in ids}
    for _ in range(1000):
        pick = swrr.select_swrr(cands, state)
        assert pick in ids
        counts[pick] += 1
    # SWRR with equal weights alternates exactly → ~50/50, within tight tolerance.
    for i in ids:
        assert 0.45 < counts[i] / 1000 < 0.55, f"{i}: {counts[i] / 1000}"


def test_swrr_weight_1_2_split_33_67() -> None:
    ids = _ids(2)
    cands = [(ids[0], 1), (ids[1], 2)]
    state = swrr.SWRRState()
    counts = {i: 0 for i in ids}
    for _ in range(3000):
        pick = swrr.select_swrr(cands, state)
        counts[pick] += 1
    # weight 1 : 2 → exactly 1/3 : 2/3 over each 3-draw cycle.
    assert abs(counts[ids[0]] / 3000 - 1 / 3) < 0.02
    assert abs(counts[ids[1]] / 3000 - 2 / 3) < 0.02


def test_swrr_weight_1_10_split() -> None:
    ids = _ids(2)
    cands = [(ids[0], 1), (ids[1], 10)]
    state = swrr.SWRRState()
    counts = {i: 0 for i in ids}
    for _ in range(11000):
        pick = swrr.select_swrr(cands, state)
        counts[pick] += 1
    assert abs(counts[ids[0]] / 11000 - 1 / 11) < 0.02
    assert abs(counts[ids[1]] / 11000 - 10 / 11) < 0.02


def test_swrr_tie_break_smallest_uuid() -> None:
    """On a weight tie, the lexicographically smallest instance UUID wins."""
    ids = sorted(_ids(3))
    cands = [(i, 1) for i in ids]
    state = swrr.SWRRState()
    # First draw with equal weights + zero starting state: all advance to weight 1,
    # tie → smallest UUID wins.
    first = swrr.select_swrr(cands, state)
    assert first == ids[0]


def test_swrr_excludes_non_eligible() -> None:
    """Members not in eligible_instance_ids don't participate or accrue weight."""
    ids = _ids(3)
    cands = [(i, 1) for i in ids]
    state = swrr.SWRRState()
    # Only ids[2] eligible → always picks ids[2], others never accrue.
    for _ in range(5):
        assert swrr.select_swrr(cands, state, eligible_instance_ids={ids[2]}) == ids[2]
    # ids[0]/ids[1] current_weights stayed at 0.
    assert state.current_weights.get(ids[0], 0) == 0
    assert state.current_weights.get(ids[1], 0) == 0


def test_swrr_returns_none_when_no_eligible() -> None:
    ids = _ids(2)
    cands = [(ids[0], 1), (ids[1], 1)]
    state = swrr.SWRRState()
    assert swrr.select_swrr(cands, state, eligible_instance_ids=set()) is None


def test_swrr_prunes_stale_members_on_generation_change() -> None:
    """When the member set changes, stale current_weights are pruned (§10.1)."""
    ids = _ids(3)
    state = swrr.SWRRState()
    # Advance with members 0,1
    swrr.select_swrr([(ids[0], 1), (ids[1], 1)], state)
    assert ids[0] in state.current_weights
    # New generation: members 1,2 only → id[0] pruned
    swrr.select_swrr([(ids[1], 1), (ids[2], 1)], state)
    assert ids[0] not in state.current_weights


def test_swrr_distribution_matches_expected() -> None:
    """Theoretical expected_distribution() matches empirical SWRR over a full cycle."""
    ids = _ids(3)
    cands = [(ids[0], 2), (ids[1], 3), (ids[2], 5)]
    expected = swrr.expected_distribution(cands, 1000)
    state = swrr.SWRRState()
    counts = {i: 0 for i in ids}
    for _ in range(1000):
        counts[swrr.select_swrr(cands, state)] += 1
    for iid, exp in expected.items():
        assert abs(counts[iid] / 1000 - exp) < 0.02, (
            f"{iid}: empirical {counts[iid] / 1000} vs expected {exp}"
        )
