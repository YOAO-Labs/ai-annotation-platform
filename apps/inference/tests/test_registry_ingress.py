"""Pure behavior tests for Registry startup and screenshot-seed ingress."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from aap_inference.pool_capability import (
    canonicalize_capability,
    capability_fingerprint,
)
from aap_inference.registry_ingress import (
    endpoint_snapshot,
    ensure_singleton_pool,
    get_or_create_seed,
    refresh_candidates,
    sync_environment,
    RegistrySeedFacts,
)
from aap_inference_contract.capabilities import CapabilityLimits


def _row(
    url: str,
    *,
    source: str = "env",
    name: str = "old-name",
    extra_params=None,
    health_meta=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        url=url,
        source=source,
        auth_method="token",
        auth_token="synthetic",
        gpu_resource_id=None,
        state="connected",
        is_interactive=True,
        health_meta=health_meta or {"old": "health"},
        last_checked_at=None,
        error_message="old-error",
        extra_params=extra_params,
    )

@pytest.mark.asyncio
async def test_screenshot_seed_cannot_take_over_plugin_registry():
    row = _row("http://plugin", source="manual", extra_params={"plugin": {"status": "active"}})
    data = _Data([row])
    with pytest.raises(ValueError, match="Plugin installations"):
        await get_or_create_seed(data, row.url, "seed", {"managed_by": "seed"})
    assert data.staged_seed_markers == []



class _Data:
    def __init__(self, rows=(), *, trace=None, cached_result=None):
        self.trace = trace if trace is not None else []
        self.rows = list(rows)
        self.pools = {}
        self.created_pool_inputs = []
        self.staged_observations = []
        self.staged_disconnects = []
        self.staged_seed_markers = []
        self.cached_result = cached_result
        self.sync_receipts = []

    async def by_url(self, url):
        self.trace.append(("by_url", url))
        return next((row for row in self.rows if row.url == url), None)

    @staticmethod
    def _seed_facts(row):
        return RegistrySeedFacts(
            id=row.id,
            name=row.name,
            url=row.url,
            state=row.state,
            is_interactive=row.is_interactive,
            source=row.source,
            health_meta=row.health_meta,
            extra_params=row.extra_params,
        )

    async def seed_by_url(self, url):
        self.trace.append(("seed_by_url", url))
        row = next((row for row in self.rows if row.url == url), None)
        return self._seed_facts(row) if row is not None else None

    async def create_seed_registry(self, name, url, marker):
        self.trace.append(("create_seed_registry", url, name))
        row = RegistrySeedFacts(
            id=uuid.uuid4(),
            name=name,
            url=url,
            state="disconnected",
            is_interactive=False,
            source="seed",
            health_meta=None,
            extra_params={"seed": marker},
        )
        self.rows.append(row)
        return row

    async def stage_seed_marker_identity(self, row, marker, previous_extra_params):
        self.trace.append(("stage_seed_marker_identity", row.url))
        self.staged_seed_markers.append((row, marker, previous_extra_params))

    async def candidates(self, profile):
        self.trace.append(("candidates", profile))
        if profile == "env":
            return [row for row in self.rows if row.source == "env"]
        return [row for row in self.rows if row.source != "seed"]

    async def create_registry(
        self,
        name,
        url,
        source,
        *,
        is_interactive=False,
        extra_params=None,
    ):
        self.trace.append(("create_registry", url, source, name, is_interactive))
        row = _row(url, source=source, name=name, extra_params=extra_params or {})
        row.state = "disconnected"
        row.is_interactive = is_interactive
        row.health_meta = None
        row.error_message = None
        self.rows.append(row)
        return row

    async def singleton_pool(self, registry_id):
        self.trace.append(("singleton_pool", registry_id))
        return self.pools.get(registry_id)

    async def create_singleton_pool(self, row, snapshot, fingerprint):
        self.trace.append(("create_singleton_pool", row.id))
        self.created_pool_inputs.append(
            (row, snapshot, fingerprint, row.health_meta, row.state, row.is_interactive)
        )
        pool = SimpleNamespace(
            id=uuid.uuid4(),
            name=row.name,
            enabled=False,
            routing_policy="smooth_weighted_round_robin",
            legacy_instance_id=row.id,
            routing_generation=1,
            capability_fingerprint=fingerprint,
            capability_snapshot=snapshot,
        )
        self.pools[row.id] = pool
        return pool

    async def stage_observation(self, row, *, profile, usable_caps, expected_snapshot):
        self.trace.append(("stage_observation", row.url, profile, usable_caps))
        self.staged_observations.append(
            {
                "row": row,
                "profile": profile,
                "usable_caps": usable_caps,
                "expected_snapshot": expected_snapshot,
            }
        )

    async def stage_env_disconnect(self, row):
        self.trace.append(("stage_env_disconnect", row.url))
        self.staged_disconnects.append(row)

    async def stage_seed_marker(self, row, marker, previous_extra_params):
        self.trace.append(("stage_seed_marker", row.url))
        self.staged_seed_markers.append((row, marker, previous_extra_params))

    async def flush(self):
        self.trace.append(("flush",))

    async def sync_result(self, request_id, command_sha256):
        self.trace.append(("sync_result", request_id, command_sha256))
        return self.cached_result

    async def record_sync_result(self, request_id, command_sha256, observed_urls):
        self.trace.append(("record_sync_result", observed_urls))
        result = {
            "request_id": str(request_id),
            "command_sha256": command_sha256,
            "observed_urls": observed_urls,
        }
        self.sync_receipts.append(result)
        return result


class _DataContext:
    def __init__(self, data, trace):
        self.data = data
        self.trace = trace

    async def __aenter__(self):
        self.trace.append(("data_enter",))
        return self.data

    async def __aexit__(self, exc_type, exc, tb):
        self.trace.append(("data_exit",))
        return False


class _ClientContext:
    def __init__(self, trace):
        self.trace = trace

    async def __aenter__(self):
        self.trace.append(("http_enter",))
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.trace.append(("http_exit",))
        return False


class _ProbeClient:
    def __init__(self, trace, *, healthy=True, meta=None, setup=None, setup_error=None):
        self.trace = trace
        self.healthy = healthy
        self.meta = meta
        self.setup_response = setup
        self.setup_error = setup_error

    async def health_meta(self):
        self.trace.append(("health_meta",))
        return self.healthy, self.meta

    async def setup(self):
        self.trace.append(("setup",))
        if self.setup_error is not None:
            raise self.setup_error
        return self.setup_response


def test_endpoint_snapshot_has_only_five_fence_values():
    row = _row("http://backend")

    assert endpoint_snapshot(row) == {
        "url": "http://backend",
        "auth_method": "token",
        "auth_token": "synthetic",
        "gpu_resource_id": None,
        "source": "env",
    }


@pytest.mark.asyncio
async def test_ensure_singleton_pool_returns_existing_without_reading_capabilities():
    row = _row("http://backend", health_meta=object())
    data = _Data([row])
    existing = SimpleNamespace(id=uuid.uuid4())
    data.pools[row.id] = existing

    assert await ensure_singleton_pool(data, row) is existing
    assert data.created_pool_inputs == []
    assert data.trace == [("singleton_pool", row.id)]


@pytest.mark.asyncio
async def test_ensure_singleton_pool_seeds_only_existing_dict_capabilities():
    caps = {"model_version": "v1", "supported_prompts": ["bbox"]}
    row = _row("http://backend", health_meta={"capabilities": caps})
    data = _Data([row])

    pool = await ensure_singleton_pool(data, row)

    expected_snapshot = canonicalize_capability(caps)
    assert pool.capability_snapshot == expected_snapshot
    assert pool.capability_fingerprint == capability_fingerprint(expected_snapshot)
    assert data.created_pool_inputs == [
        (
            row,
            expected_snapshot,
            pool.capability_fingerprint,
            row.health_meta,
            row.state,
            row.is_interactive,
        )
    ]


@pytest.mark.asyncio
async def test_seed_marker_is_staged_before_singleton_lookup_and_preserves_extra():
    previous = {"owner": "fixture", "other": 3}
    row = _row("http://stub", source="seed", extra_params=previous)
    data = _Data([row])
    marker = {"managed_by": "screenshot-seed", "profile": "screenshots"}

    result = await get_or_create_seed(data, "http://stub/", "stub", marker)

    assert result.id == row.id
    assert result.url == "http://stub"
    assert result.extra_params == {"owner": "fixture", "other": 3, "seed": marker}
    staged_row, staged_marker, staged_previous = data.staged_seed_markers[0]
    assert staged_row.id == row.id
    assert staged_marker == marker
    assert staged_previous == previous
    assert [event[0] for event in data.trace] == [
        "seed_by_url",
        "stage_seed_marker_identity",
        "singleton_pool",
        "create_singleton_pool",
    ]


@pytest.mark.asyncio
async def test_seed_marker_keeps_legacy_mapping_error_before_staging():
    row = _row("http://stub", source="seed", extra_params=["invalid-object"])
    data = _Data([row])
    marker = {"managed_by": "screenshot-seed", "profile": "screenshots"}

    with pytest.raises(TypeError, match="mapping"):
        await get_or_create_seed(data, "http://stub", "stub", marker)

    assert row.extra_params == ["invalid-object"]
    assert data.staged_seed_markers == []
    assert data.trace == [("seed_by_url", "http://stub")]


@pytest.mark.asyncio
async def test_new_seed_is_inserted_with_marker_before_pool_creation():
    data = _Data()
    marker = {"managed_by": "screenshot-seed", "profile": "screenshots"}

    row = await get_or_create_seed(data, "http://stub/", "stub", marker)

    assert row.source == "seed"
    assert row.url == "http://stub"
    assert row.extra_params == {"seed": marker}
    assert data.staged_seed_markers == []
    assert data.created_pool_inputs[0] == (
        row,
        None,
        None,
        None,
        "disconnected",
        False,
    )
    assert [event[0] for event in data.trace] == [
        "seed_by_url",
        "create_seed_registry",
        "singleton_pool",
        "create_singleton_pool",
    ]


@pytest.mark.asyncio
async def test_seed_refresh_captures_snapshot_before_probe_and_stages_in_order():
    trace = []
    row = _row("http://seed", source="seed")
    data = _Data([row], trace=trace)
    old_snapshot = endpoint_snapshot(row)
    observed_at = datetime(2026, 10, 22, tzinfo=UTC)
    setup = {"name": 42, "supported_prompts": ["bbox"]}
    client = _ProbeClient(trace, meta={"gpu_info": "fixture"}, setup=setup)

    def make_client(backend):
        trace.append(("client_factory", backend.id))
        backend.url = "http://mutated-during-probe"
        return client

    def limits():
        trace.append(("limits",))
        return CapabilityLimits()

    result = await refresh_candidates(
        data,
        [row],
        client_factory=make_client,
        capability_limits_factory=limits,
        clock=lambda: trace.append(("clock",)) or observed_at,
    )

    assert result == [row]
    assert row.name == "42"
    assert row.state == "connected"
    assert row.error_message is None
    assert row.last_checked_at == observed_at
    assert row.health_meta["gpu_info"] == "fixture"
    stage = data.staged_observations[0]
    assert stage["profile"] == "seed"
    assert stage["usable_caps"] is True
    assert stage["expected_snapshot"] == old_snapshot
    assert [event[0] for event in trace] == [
        "client_factory",
        "health_meta",
        "setup",
        "clock",
        "limits",
        "stage_observation",
    ]


@pytest.mark.asyncio
async def test_seed_probe_failure_only_changes_state_error_and_timestamp():
    row = _row("http://seed", source="seed")
    original_name = row.name
    original_interactive = row.is_interactive
    original_health = row.health_meta
    data = _Data([row])
    observed_at = datetime(2026, 10, 22, tzinfo=UTC)

    def make_client(_backend):
        return _ProbeClient([], healthy=False, meta={"ignored": True})

    await refresh_candidates(
        data,
        [row],
        client_factory=make_client,
        capability_limits_factory=CapabilityLimits,
        clock=lambda: observed_at,
    )

    assert row.state == "error"
    assert row.error_message == "health check failed"
    assert row.last_checked_at == observed_at
    assert row.name == original_name
    assert row.is_interactive is original_interactive
    assert row.health_meta is original_health
    assert data.staged_observations[0]["usable_caps"] is False


@pytest.mark.asyncio
async def test_seed_client_constructor_failure_is_not_fail_opened():
    row = _row("http://seed", source="seed")
    data = _Data([row])

    def make_client(_backend):
        raise LookupError("constructor failed")

    with pytest.raises(LookupError, match="constructor failed"):
        await refresh_candidates(
            data,
            [row],
            client_factory=make_client,
            capability_limits_factory=CapabilityLimits,
            clock=lambda: datetime(2026, 10, 22, tzinfo=UTC),
        )

    assert data.staged_observations == []


@pytest.mark.asyncio
async def test_seed_setup_failure_is_stored_without_limits_or_metadata_replacement():
    trace = []
    row = _row("http://seed", source="seed")
    original_name = row.name
    original_health = row.health_meta
    data = _Data([row], trace=trace)
    observed_at = datetime(2026, 10, 22, tzinfo=UTC)

    def make_client(_backend):
        trace.append(("client_factory",))
        return _ProbeClient(
            trace,
            healthy=True,
            meta={"compute": "new"},
            setup_error=RuntimeError("setup unavailable"),
        )

    def limits():
        pytest.fail("setup failure must prevent capability-limit lookup")

    await refresh_candidates(
        data,
        [row],
        client_factory=make_client,
        capability_limits_factory=limits,
        clock=lambda: trace.append(("clock",)) or observed_at,
    )

    assert row.state == "error"
    assert row.error_message == "RuntimeError: setup unavailable"
    assert row.last_checked_at == observed_at
    assert row.name == original_name
    assert row.health_meta is original_health
    assert data.staged_observations[0]["usable_caps"] is False
    assert [event[0] for event in trace] == [
        "client_factory",
        "health_meta",
        "setup",
        "clock",
        "stage_observation",
    ]


@pytest.mark.asyncio
async def test_environment_observes_http_before_data_and_limits_before_manual_skip():
    trace = []
    manual = _row("http://manual", source="manual")
    existing = _row("http://existing", source="env")
    stale = _row("http://stale", source="env")
    data = _Data([manual, existing, stale], trace=trace)
    setup_by_url = {
        "http://manual": {"name": "manual probe", "supported_prompts": ["bbox"]},
        "http://existing": {"name": "env probe", "supported_prompts": ["bbox"]},
        "http://new": RuntimeError("network exception"),
    }

    def make_http_client(timeout_seconds):
        trace.append(("client_factory", timeout_seconds))
        return _ClientContext(trace)

    async def setup_probe(_client, url):
        trace.append(("http", url))
        result = setup_by_url[url]
        if isinstance(result, BaseException):
            raise result
        return result

    def limits():
        trace.append(("limits",))
        return CapabilityLimits()

    def data_factory():
        trace.append(("data_factory",))
        return _DataContext(data, trace)

    observed_at = datetime(2026, 10, 22, tzinfo=UTC)
    count = await sync_environment(
        ["http://manual", "http://existing", "http://new"],
        timeout_seconds=2,
        data_factory=data_factory,
        capability_limits_factory=limits,
        client_factory=make_http_client,
        setup_probe=setup_probe,
        clock=lambda: trace.append(("clock",)) or observed_at,
    )

    assert count == 3
    http_positions = [i for i, event in enumerate(trace) if event[0] == "http"]
    assert len(http_positions) == 3
    assert (
        max(http_positions) < trace.index(("clock",)) < trace.index(("data_factory",))
    )
    assert trace.index(("limits",)) < trace.index(("by_url", "http://manual"))
    assert manual.name == "old-name"
    assert existing.name == "env probe"
    assert existing.state == "connected"
    assert existing.error_message == "old-error"
    assert existing.last_checked_at == observed_at
    new = next(row for row in data.rows if row.url == "http://new")
    assert new.name == "new"
    assert new.state == "error"
    assert new.health_meta is None
    assert new.error_message is None
    assert stale.state == "disconnected"
    assert stale.last_checked_at is None
    assert [stage["row"] for stage in data.staged_observations] == [existing, new]
    assert data.staged_disconnects == [stale]
    new_lookups = [
        i for i, event in enumerate(trace) if event == ("by_url", "http://new")
    ]
    assert len(new_lookups) == 2
    assert new_lookups[0] < trace.index(
        ("create_registry", "http://new", "env", "new", False)
    )
    assert (
        trace.index(("create_singleton_pool", new.id))
        < new_lookups[1]
        < trace.index(("stage_observation", "http://new", "env", False))
    )
    new_pool_input = next(
        values for values in data.created_pool_inputs if values[0] is new
    )
    assert new_pool_input[3:] == (None, "disconnected", False)
    assert trace[-2:] == [("flush",), ("data_exit",)]


@pytest.mark.asyncio
async def test_environment_receipt_replay_repeats_probe_but_skips_all_mutations():
    trace = []
    data = _Data(cached_result={"state": "committed", "observed_urls": 1}, trace=trace)
    request_id = uuid.uuid4()

    def data_factory():
        trace.append(("data_factory",))
        return _DataContext(data, trace)

    result = await sync_environment(
        ["http://backend"],
        timeout_seconds=1,
        data_factory=data_factory,
        capability_limits_factory=CapabilityLimits,
        client_factory=lambda _timeout: _ClientContext(trace),
        setup_probe=lambda _client, url: _immediate(trace, url),
        clock=lambda: trace.append(("clock",)) or datetime(2026, 10, 22, tzinfo=UTC),
        request_id=request_id,
        command_sha256="a" * 64,
    )

    assert result == data.cached_result
    assert data.staged_observations == []
    assert data.staged_disconnects == []
    assert data.trace[-2:] == [("sync_result", request_id, "a" * 64), ("data_exit",)]
    assert ("flush",) not in data.trace
    assert trace.index(("http", "http://backend")) < trace.index(("clock",))
    assert trace.index(("clock",)) < trace.index(("data_factory",))


@pytest.mark.asyncio
async def test_empty_environment_disconnects_all_rows_and_flushes_once():
    trace = []
    stale = _row("http://stale", source="env")
    data = _Data([stale], trace=trace)

    def data_factory():
        trace.append(("data_factory",))
        return _DataContext(data, trace)

    count = await sync_environment(
        [],
        timeout_seconds=1,
        data_factory=data_factory,
        capability_limits_factory=CapabilityLimits,
        client_factory=lambda _timeout: _ClientContext(trace),
        setup_probe=lambda _client, url: _immediate(trace, url),
        clock=lambda: trace.append(("clock",)) or datetime(2026, 10, 22, tzinfo=UTC),
    )

    assert count == 0
    assert stale.state == "disconnected"
    assert data.staged_disconnects == [stale]
    assert trace.index(("http_exit",)) < trace.index(("clock",))
    assert trace.index(("clock",)) < trace.index(("data_factory",))
    assert trace[-2:] == [("flush",), ("data_exit",)]


async def _immediate(trace, url):
    trace.append(("http", url))
    return None
