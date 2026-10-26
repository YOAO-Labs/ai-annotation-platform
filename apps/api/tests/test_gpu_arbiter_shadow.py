from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from aap_inference.gpu.diagnostics import record_unregistered_gpu_shadow_dispatch
from aap_inference.gpu.policy import (
    effective_gpu_arbiter_mode,
    evaluate_gpu_shadow_decision,
    gpu_shadow_observation_enabled,
)
from aap_inference.gpu.settings import (
    GPUArbiterMode,
    GPUAuthoritySettings,
    parse_gpu_resources,
)


NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def _config(
    *,
    global_mode: str = "observe",
    resources: tuple[tuple[str, int, str | None], ...] = (
        ("node-a/index:0", 20_000, "observe"),
    ),
) -> GPUAuthoritySettings:
    rows = []
    for resource_id, allocatable_mb, mode in resources:
        node_id, physical_device_token = resource_id.split("/", 1)
        mode_field = f',"mode":"{mode}"' if mode is not None else ""
        rows.append(
            f'"{resource_id}":{{"node_id":"{node_id}",'
            f'"physical_device_token":"{physical_device_token}",'
            f'"allocatable_mb":{allocatable_mb}{mode_field}}}'
        )
    resource_map, errors = parse_gpu_resources("{" + ",".join(rows) + "}")
    return GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode(global_mode),
        gpu_arbiter_resources=resource_map,
        gpu_arbiter_config_errors=errors,
    )


def _residency(
    backend_id: uuid.UUID,
    resource_id: str,
    *,
    gpu_loaded: bool | None = True,
    active_requests: int | None = 0,
    builders: int | None = 0,
    borrowers: int | None = 0,
    draining: bool | None = False,
    evictable: bool | None = True,
    generation: str | None = "7",
    state: str = "resident",
    lifecycle_gate: str | None = "enforce",
    identity: dict | None = None,
    pools: dict | None = None,
) -> dict:
    if identity is None:
        identity = {
            "audience": "aap-gpu-lifecycle",
            "backend_registry_id": str(backend_id),
            "gpu_resource_id": resource_id,
        }
    if pools is None:
        pools = {
            "models": {
                "resident": gpu_loaded,
                "device": "cuda:0" if gpu_loaded else None,
                "provider": None,
            }
        }
    return {
        "state": state,
        "gpu_loaded": gpu_loaded,
        "active_requests": active_requests,
        "builders": builders,
        "borrowers": borrowers,
        "draining": draining,
        "evictable": evictable,
        "generation": generation,
        "boot_id": "boot-1",
        "control_epoch": "1",
        "lifecycle_gate": lifecycle_gate,
        "identity": identity,
        "pools": pools,
    }


def _backend(
    *,
    resource_id: str | None = "node-a/index:0",
    budget_mb: int | None = 8_000,
    priority: int = 0,
    gpu_loaded: bool | None = True,
    age: timedelta = timedelta(seconds=30),
    state: str = "connected",
    compute: dict | None = None,
    residency_overrides: dict | None = None,
) -> SimpleNamespace:
    backend_id = uuid.uuid4()
    residency = (
        _residency(backend_id, resource_id, gpu_loaded=gpu_loaded)
        if resource_id is not None
        else None
    )
    if residency is not None and residency_overrides:
        residency.update(residency_overrides)
    health_meta: dict = {}
    if compute is not None:
        health_meta["compute"] = compute
    if residency is not None:
        health_meta["residency"] = residency
    return SimpleNamespace(
        id=backend_id,
        state=state,
        last_checked_at=NOW - age,
        health_meta=health_meta,
        gpu_resource_id=resource_id,
        vram_budget_mb=budget_mb,
        eviction_priority=priority,
    )


@pytest.mark.parametrize(
    ("global_mode", "resource_mode", "expected"),
    [
        ("off", "observe", GPUArbiterMode.OFF),
        ("observe", "observe", GPUArbiterMode.OBSERVE),
        ("observe", "enforce", GPUArbiterMode.OBSERVE),
        ("enforce", "observe", GPUArbiterMode.OBSERVE),
        ("enforce", "enforce", GPUArbiterMode.OFF),
        ("enforce", None, GPUArbiterMode.OFF),
    ],
)
def test_effective_mode_only_promotes_observe_runtime(
    global_mode: str,
    resource_mode: str | None,
    expected: GPUArbiterMode,
) -> None:
    config = _config(
        global_mode=global_mode, resources=(("node-a/index:0", 20_000, resource_mode),)
    )

    assert effective_gpu_arbiter_mode("node-a/index:0", config=config) is expected


@pytest.mark.parametrize(
    ("peer_budget", "expected"),
    [(11_999, "would-admit"), (12_000, "would-admit"), (12_001, "would-reject")],
)
def test_shadow_capacity_handles_below_equal_and_one_mib_over(
    peer_budget: int, expected: str
) -> None:
    requester = _backend(gpu_loaded=False, budget_mb=8_000)
    peer = _backend(budget_mb=peer_budget, residency_overrides={"evictable": False})

    decision = evaluate_gpu_shadow_decision(
        requester,
        [peer, requester],
        operation="predict",
        config=_config(),
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == expected
    assert decision.projected_mb == peer_budget + 8_000
    assert decision.authoritative is False


def test_shadow_reports_safe_candidate_set_without_claiming_lru_order() -> None:
    requester = _backend(gpu_loaded=False, budget_mb=10_000, priority=5)
    low = _backend(budget_mb=6_000, priority=1)
    equal = _backend(budget_mb=5_000, priority=5)
    higher = _backend(budget_mb=9_000, priority=6)

    decision = evaluate_gpu_shadow_decision(
        requester,
        [higher, equal, requester, low],
        operation="warmup",
        config=_config(),
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == "would-evict"
    assert {candidate.backend_id for candidate in decision.candidates} == {
        str(low.id),
        str(equal.id),
    }
    assert decision.candidate_order_authoritative is False
    assert str(higher.id) not in {c.backend_id for c in decision.candidates}


@pytest.mark.parametrize(
    "overrides",
    [
        {"active_requests": 1},
        {"active_requests": False},
        {"builders": 1},
        {"builders": False},
        {"borrowers": 1},
        {"borrowers": False},
        {"draining": True},
        {"evictable": False},
        {"generation": None},
        {"generation": "01"},
        {"boot_id": None},
        {"control_epoch": "01"},
        {"state": "loading"},
        {"lifecycle_gate": "legacy"},
        {"identity": None},
        {
            "identity": {
                "audience": "aap-gpu-lifecycle",
                "backend_registry_id": "wrong",
                "gpu_resource_id": "node-a/index:0",
            }
        },
    ],
)
def test_shadow_candidate_safety_matrix(overrides: dict) -> None:
    requester = _backend(gpu_loaded=False, budget_mb=15_000, priority=5)
    victim = _backend(budget_mb=10_000, priority=0)
    if overrides.get("identity", "not-set") is None:
        victim.health_meta["residency"]["identity"] = None
    else:
        victim.health_meta["residency"].update(overrides)

    decision = evaluate_gpu_shadow_decision(
        requester,
        [requester, victim],
        operation="reload",
        config=_config(),
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == "would-reject"
    assert decision.candidates == ()


def test_stale_or_malformed_empty_residency_stays_conservatively_committed() -> None:
    requester = _backend(gpu_loaded=False, budget_mb=8_000)
    stale = _backend(gpu_loaded=False, budget_mb=9_000, age=timedelta(minutes=4))
    malformed = _backend(
        gpu_loaded=False,
        budget_mb=4_000,
        residency_overrides={
            "builders": None,
            "pools": {"models": {"resident": None}},
        },
    )

    decision = evaluate_gpu_shadow_decision(
        requester,
        [requester, stale, malformed],
        operation="predict",
        config=_config(),
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == "would-reject"
    assert decision.committed_before_mb == 13_000
    assert set(decision.uncertain_backend_ids) == {str(stale.id), str(malformed.id)}


def test_single_card_multi_card_and_same_index_on_other_host_are_isolated() -> None:
    config = _config(
        resources=(
            ("node-a/index:0", 20_000, "observe"),
            ("node-a/index:1", 20_000, "observe"),
            ("node-b/index:0", 20_000, "observe"),
        )
    )
    requester = _backend(gpu_loaded=False, budget_mb=10_000)
    same_card = _backend(budget_mb=9_000)
    other_card = _backend(resource_id="node-a/index:1", budget_mb=20_000)
    other_host_same_index = _backend(resource_id="node-b/index:0", budget_mb=20_000)

    decision = evaluate_gpu_shadow_decision(
        requester,
        [other_host_same_index, other_card, same_card, requester],
        operation="predict_interactive",
        config=config,
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == "would-admit"
    assert decision.committed_before_mb == 9_000
    assert decision.projected_mb == 19_000


def test_explicit_cpu_without_claim_is_skipped_but_mixed_cpu_gpu_is_charged() -> None:
    explicit_cpu = _backend(
        resource_id=None,
        budget_mb=None,
        compute={"configured_device": "cpu", "effective_device": "cpu"},
    )
    assert (
        evaluate_gpu_shadow_decision(
            explicit_cpu,
            [explicit_cpu],
            operation="predict",
            config=_config(),
            now=NOW,
        )
        is None
    )

    requester = _backend(gpu_loaded=False, budget_mb=12_000)
    mixed = _backend(
        gpu_loaded=True,
        budget_mb=9_000,
        compute={"configured_device": "cuda", "effective_device": "cpu"},
        residency_overrides={"evictable": False},
    )
    decision = evaluate_gpu_shadow_decision(
        requester,
        [requester, mixed],
        operation="predict",
        config=_config(),
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == "would-reject"
    assert decision.committed_before_mb == 9_000


def test_missing_gpu_claim_in_observe_is_reported_as_non_authoritative_reject() -> None:
    unknown = _backend(resource_id=None, budget_mb=None, compute=None)

    decision = evaluate_gpu_shadow_decision(
        unknown,
        [unknown],
        operation="predict",
        config=_config(),
        now=NOW,
    )

    assert decision is not None
    assert decision.decision == "would-reject"
    assert decision.reason == "gpu_claim_missing_or_unverified"
    assert decision.authoritative is False
    assert decision.global_mode == "observe"
    assert decision.desired_mode == "off"
    assert decision.effective_mode == "off"


def test_off_fast_path_does_not_parse_resource_configuration() -> None:
    class OffConfig:
        gpu_arbiter_mode = GPUArbiterMode.OFF

        @property
        def gpu_arbiter_resources(self):
            raise AssertionError("off mode must not parse resources")

    assert not gpu_shadow_observation_enabled(
        "node-a/index:0",
        config=OffConfig(),  # type: ignore[arg-type]
    )


def test_unregistered_unload_is_not_logged_as_would_reject() -> None:
    with capture_logs() as logs:
        record_unregistered_gpu_shadow_dispatch(
            "http://legacy-backend:9000", "unload", config=_config()
        )

    record = next(
        item
        for item in logs
        if item["event"] == "gpu_arbiter_shadow_unregistered_unload"
    )
    assert "decision" not in record["gpu_arbiter"]
    assert record["gpu_arbiter"]["releases_allocation"] is False
