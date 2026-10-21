"""Exercise actual API HTTP assembly against the independent execution port."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid

import httpx
import pytest

from aap_inference_contract.routing import ROUTING_DATA_PROTOCOL, RoutingScopeView
from app.config import GPUArbiterMode, settings
from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_client import MLBackendClient
from app.services.ml_routing.authority import RoutingRejected, scope_binding
from app.services.ml_routing.contracts import RoutingError, RejectionReason

REAL_CLIENT = httpx.AsyncClient


class Authority:
    def __init__(self, registry_id, *, reject=False):
        self.trace = []
        self.issuer_open = False
        self.reject = reject
        self.scope = RoutingScopeView(
            id=uuid.uuid4(),
            pool_id=None,
            registry_id=registry_id,
            project_id=None,
            mode="direct",
            owner="api",
            operation="backend_http",
            generation=None,
            config_revision=1,
            eligibility_revision=1,
            authority_epoch=uuid.uuid4(),
            state="reserved",
            closed=False,
            heartbeat_deadline_ms=1000,
            capability="opaque-capability",
        )

    async def acquire(self, **kwargs):
        self.trace.append(("acquire", kwargs))
        registry_id = kwargs.get("registry_id", self.scope.registry_id)
        return (
            self.scope
            if registry_id == self.scope.registry_id
            else self.scope.model_copy(
                update={"id": uuid.uuid4(), "registry_id": registry_id}
            )
        )

    @asynccontextmanager
    async def data_transaction(self):
        assert not self.issuer_open
        self.issuer_open = True
        outer = self

        class Data:
            async def step(self, action, body):
                assert action == "capture_transport_subject"
                outer.trace.append(("subject", body))
                return {
                    "schema": ROUTING_DATA_PROTOCOL,
                    "status": "ok",
                    "subject": "encrypted-digest",
                }

        try:
            yield Data()
        finally:
            self.issuer_open = False

    async def execute(self, operation, body):
        self.trace.append((operation, body))
        if operation == "begin":
            assert self.issuer_open
            assert "transport_identity" not in body
            assert body["transport_subject"] == "encrypted-digest"
            if self.reject:
                raise RoutingRejected("generation_mismatch")
            return SimpleNamespace(headers={"X-AAP-Route-Admission": "bound-admission"})
        assert operation == "settle"
        assert not self.issuer_open

    async def close(self, scope):
        self.trace.append(("close", scope.id))


def client(monkeypatch, *, authority, handler, bind_routing=True, session_factory=None):
    monkeypatch.setattr(settings, "gpu_arbiter_mode", GPUArbiterMode.OFF)
    monkeypatch.setattr(settings, "gpu_arbiter_rollout_enabled", False)
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: REAL_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )
    backend = MLBackendRegistry(
        id=authority.scope.registry_id,
        name="CPU",
        url="http://backend.test/",
        auth_method="token",
        auth_token="secret",
        extra_params={"device": "cpu", "max_concurrency": 2},
    )
    return MLBackendClient(
        backend,
        routing_authority=authority if bind_routing else None,
        shadow_session_factory=session_factory,
    ), backend


@pytest.mark.asyncio
async def test_actual_direct_prediction_captures_cached_transport_then_closes_issuer_before_http(
    monkeypatch,
):
    authority = Authority(uuid.uuid4())

    async def handler(request):
        assert not authority.issuer_open
        assert request.headers["Authorization"] == "Bearer secret"
        assert request.headers["X-AAP-Route-Admission"] == "bound-admission"
        authority.trace.append(("http", request.url.path))
        return httpx.Response(200, json={"results": [{"task": "one", "result": []}]})

    transport, backend = client(monkeypatch, authority=authority, handler=handler)
    backend.auth_token = "changed-after-construction"
    backend.extra_params = {"device": "changed"}
    result = await transport.predict([{"id": "one", "data": {}}])
    assert result[0].task_id == "one"
    identity = next(
        body["transport_identity"] for op, body in authority.trace if op == "subject"
    )
    assert (
        identity["url"] == "http://backend.test/" and identity["auth_token"] == "secret"
    )
    assert identity["extra_params"] == {"device": "cpu", "max_concurrency": 2}
    assert [op for op, _ in authority.trace] == [
        "acquire",
        "subject",
        "begin",
        "http",
        "settle",
        "close",
    ]
    settled = next(body for op, body in authority.trace if op == "settle")
    assert (settled["kind"], settled["status_code"]) == ("response", 200)


@pytest.mark.asyncio
async def test_pinned_scope_is_reused_across_real_requests_without_per_call_close(
    monkeypatch,
):
    authority = Authority(uuid.uuid4())

    async def handler(_request):
        return httpx.Response(200, json={"results": [{"task": "one", "result": []}]})

    transport, _ = client(monkeypatch, authority=authority, handler=handler)
    async with scope_binding(authority, authority.scope):
        await transport.predict([{"id": "one", "data": {}}])
        await transport.predict([{"id": "one", "data": {}}])
    assert [op for op, _ in authority.trace] == [
        "subject",
        "begin",
        "settle",
        "subject",
        "begin",
        "settle",
    ]
    attempts = [body["attempt_id"] for op, body in authority.trace if op == "begin"]
    assert len(set(attempts)) == 2


@pytest.mark.asyncio
async def test_rejected_final_admission_sends_no_backend_http(monkeypatch):
    authority = Authority(uuid.uuid4(), reject=True)

    async def handler(_request):
        pytest.fail("rejected admission must not execute inference")

    transport, _ = client(monkeypatch, authority=authority, handler=handler)
    with pytest.raises(RoutingError) as error:
        await transport.predict([{"id": "one", "data": {}}])
    assert error.value.reason == RejectionReason.GENERATION_MISMATCH
    assert not authority.issuer_open
    assert [op for op, _ in authority.trace] == ["acquire", "subject", "begin", "close"]


@pytest.mark.asyncio
async def test_incidental_backend_call_gets_its_own_scope_from_the_same_owner(
    monkeypatch,
):
    source = Authority(uuid.uuid4())
    target = Authority(uuid.uuid4())

    async def handler(_request):
        return httpx.Response(200, json={"results": [{"task": "one", "result": []}]})

    transport, _ = client(monkeypatch, authority=target, handler=handler)
    async with scope_binding(source, source.scope):
        await transport.predict([{"id": "one", "data": {}}])
    assert target.trace == []
    assert [op for op, _ in source.trace] == [
        "acquire",
        "subject",
        "begin",
        "settle",
        "close",
    ]
    requested = source.trace[0][1]["registry_id"]
    assert requested == target.scope.registry_id
    begun_scope_id = next(
        body["scope_id"] for op, body in source.trace if op == "begin"
    )
    assert begun_scope_id != str(source.scope.id)
    assert source.trace[-1][1] != source.scope.id


@pytest.mark.asyncio
async def test_detached_control_row_uses_explicit_session_source_before_http(
    monkeypatch,
):
    from app.services.ml_routing import authority as module

    bound = Authority(uuid.uuid4())
    source_engine = object()
    events = []

    def make_authority(engine=None):
        if engine is None:
            return SimpleNamespace(engine=None)
        assert engine is source_engine
        return bound

    @asynccontextmanager
    async def source_session():
        events.append("source.enter")
        yield SimpleNamespace(bind=source_engine)
        events.append("source.close")

    async def handler(_request):
        events.append("http")
        assert events[:2] == ["source.enter", "source.close"]
        return httpx.Response(200, json={"results": [{"task": "one", "result": []}]})

    monkeypatch.setattr(module, "RoutingAuthority", make_authority)
    transport, _ = client(
        monkeypatch,
        authority=bound,
        handler=handler,
        bind_routing=False,
        session_factory=source_session,
    )
    await transport.predict([{"id": "one", "data": {}}])
    assert events == ["source.enter", "source.close", "http"]
    assert [op for op, _ in bound.trace] == [
        "acquire",
        "subject",
        "begin",
        "settle",
        "close",
    ]
