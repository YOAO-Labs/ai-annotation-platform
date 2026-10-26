"""Real snapshot setup I/O through the independent HTTP probe owner."""

from __future__ import annotations

from contextlib import AsyncExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BackendProbeLegacyFailure,
    BackendProbeUnavailable,
)
from app.config import settings
from app.services.inference_backend_probe import backend_probe
from tests.inference_backend_probe_helpers import probe_provider
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


@contextmanager
def setup_backend():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(
                (
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("Cache-Control"),
                )
            )
            body = b'{"supported_prompts":["bbox"],"supported_trackers":["sam2_video"]}'
            self.send_response(503 if self.path.startswith("/down/") else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


async def _binding(stack, monkeypatch, test_engine):
    """Select the only remaining probe owner: the independent HTTP process."""
    for name in (
        "inference_backend_probe_url",
        "inference_backend_probe_token",
        "inference_backend_probe_token_file",
    ):
        monkeypatch.setattr(settings, name, "")
    origin, token = await stack.enter_async_context(probe_provider(test_engine.url))
    monkeypatch.setattr(settings, "inference_backend_probe_url", origin)
    monkeypatch.setattr(settings, "inference_backend_probe_token", token)


async def _insert(db, registry_id, url, *, extra="{}"):
    await db.execute(
        text(
            "INSERT INTO public.ml_backend_registry "
            "(id,name,url,state,is_interactive,auth_method,auth_token,extra_params,source) "
            "VALUES (:id,:name,:url,'disconnected',false,'token','snapshot-secret',CAST(:extra AS jsonb),'manual')"
        ),
        {"id": registry_id, "name": str(registry_id), "url": url, "extra": extra},
    )


async def _subject(db, registry_id):
    await db.execute(text("SET LOCAL ROLE workspace_code"))
    value = await db.scalar(
        text(
            "SELECT aap_inference_v1.workspace_configuration_backend(CAST(:id AS uuid))"
        ),
        {"id": registry_id},
    )
    facts = json.loads(value) if isinstance(value, str) else value
    assert set(facts["backend"]) == {"id", "probe_subject"}
    await db.execute(text("SET LOCAL ROLE legacy_runtime"))
    return facts["backend"]["probe_subject"]


async def _probe(db, registry_id, subject):
    return await backend_probe(db)(
        registry_id, subject, timeout=2, limits=CapabilityLimits()
    )


async def test_uncommitted_snapshot_probes_without_committing_caller_and_expires(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    registry_id = uuid.uuid4()
    async with AsyncExitStack() as stack:
        await _binding(stack, monkeypatch, test_engine)
        with setup_backend() as (url, calls):
            async with maker() as caller:
                await _insert(caller, registry_id, url + "/original")
                subject = await _subject(caller, registry_id)
                async with maker() as observer:
                    assert (
                        await observer.scalar(
                            text(
                                "SELECT count(*) FROM public.ml_backend_registry WHERE id=:id"
                            ),
                            {"id": registry_id},
                        )
                        == 0
                    )
                assert await _probe(caller, registry_id, subject) == ["image", "video"]
                assert caller.in_transaction()
                async with maker() as observer:
                    assert (
                        await observer.scalar(
                            text(
                                "SELECT count(*) FROM public.ml_backend_registry WHERE id=:id"
                            ),
                            {"id": registry_id},
                        )
                        == 0
                    )
                assert calls == [
                    ("/original/setup", "Bearer snapshot-secret", "no-cache")
                ]
                await caller.rollback()
                with pytest.raises(BackendProbeUnavailable):
                    await _probe(caller, registry_id, subject)
                assert len(calls) == 1


async def test_rotation_after_snapshot_does_not_lock_or_probe_current_endpoint(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    registry_id = uuid.uuid4()
    async with AsyncExitStack() as stack:
        await _binding(stack, monkeypatch, test_engine)
        with setup_backend() as (url, calls):
            async with maker.begin() as seed:
                await _insert(seed, registry_id, url + "/old")
            try:
                async with maker() as caller:
                    subject = await _subject(caller, registry_id)
                    async with maker.begin() as writer:
                        await writer.execute(text("SET LOCAL lock_timeout='500ms'"))
                        await writer.execute(
                            text(
                                "SELECT id FROM public.ml_backend_registry WHERE id=:id FOR UPDATE NOWAIT"
                            ),
                            {"id": registry_id},
                        )
                        await writer.execute(
                            text(
                                "UPDATE public.ml_backend_registry SET url=:url,auth_token='rotated-secret' WHERE id=:id"
                            ),
                            {"id": registry_id, "url": url + "/new"},
                        )
                    assert await _probe(caller, registry_id, subject) == [
                        "image",
                        "video",
                    ]
                    assert calls == [
                        ("/old/setup", "Bearer snapshot-secret", "no-cache")
                    ]
                    await caller.commit()
                    with pytest.raises(BackendProbeUnavailable):
                        await _probe(caller, registry_id, subject)
                    assert len(calls) == 1
            finally:
                async with maker.begin() as cleanup:
                    await cleanup.execute(
                        text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                        {"id": registry_id},
                    )


async def test_legacy_json_materialization_failure_is_not_fail_open(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    registry_id = uuid.uuid4()
    async with AsyncExitStack() as stack:
        await _binding(stack, monkeypatch, test_engine)
        with setup_backend() as (url, calls):
            async with maker() as caller:
                await _insert(
                    caller, registry_id, url, extra='{"n":' + "9" * 5000 + "}"
                )
                subject = await _subject(caller, registry_id)
                with pytest.raises(BackendProbeLegacyFailure):
                    await _probe(caller, registry_id, subject)
                assert calls == []
                assert caller.in_transaction()
                await caller.rollback()


async def test_actual_backend_failure_stays_fail_open(test_engine, monkeypatch):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await _binding(stack, monkeypatch, test_engine)
        with setup_backend() as (url, calls):
            async with maker() as caller:
                registry_id = uuid.uuid4()
                await _insert(caller, registry_id, url + "/down")
                subject = await _subject(caller, registry_id)
                assert await _probe(caller, registry_id, subject) is None
                assert len(calls) == 1
                await caller.rollback()


async def test_explicit_http_credential_failure_is_unavailable_without_backend_calls(
    test_engine, monkeypatch
):
    await assert_owned_test_database(test_engine)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with AsyncExitStack() as stack:
        await _binding(stack, monkeypatch, test_engine)
        monkeypatch.setattr(
            settings, "inference_backend_probe_token", "wrong-dedicated-token"
        )
        with setup_backend() as (url, calls):
            async with maker() as caller:
                registry_id = uuid.uuid4()
                await _insert(caller, registry_id, url)
                subject = await _subject(caller, registry_id)
                with pytest.raises(BackendProbeUnavailable):
                    await _probe(caller, registry_id, subject)
                assert calls == [] and caller.in_transaction()
                await caller.rollback()


@pytest.mark.parametrize("configuration_binding", ["borrowed", "dsn", "http"])
async def test_missing_probe_secret_is_503_before_public_configuration_writes(
    test_engine, httpx_client, monkeypatch, configuration_binding
):
    from app.core.security import create_access_token
    from tests.factory import create_user
    from tests.workspace_configuration_helpers import (
        api_configuration_binding,
        configuration_dsn,
        provider_url,
    )
    from tests.test_workspace_configuration_live import _suppress_request_audit

    await assert_owned_test_database(test_engine)
    _suppress_request_audit(monkeypatch)
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    actor_id = registry_id = None
    retained_key = None
    async with AsyncExitStack() as stack:
        await _binding(stack, monkeypatch, test_engine)
        for name in (
            "workspace_configuration_url",
            "workspace_configuration_database_url",
            "workspace_configuration_database_url_file",
        ):
            monkeypatch.setattr(settings, name, "")
        if configuration_binding == "dsn":
            await stack.enter_async_context(
                api_configuration_binding(
                    dsn=configuration_dsn(test_engine), token="workspace-test-control"
                )
            )
        elif configuration_binding == "http":
            origin = await stack.enter_async_context(
                provider_url(configuration_dsn(test_engine), "workspace-test-control")
            )
            await stack.enter_async_context(
                api_configuration_binding(url=origin, token="workspace-test-control")
            )
        try:
            async with maker.begin() as seed:
                actor = await create_user(
                    seed,
                    "project_admin",
                    f"probe-missing-key-{uuid.uuid4().hex}@example.test",
                    "Probe capability failure actor",
                )
                actor_id = actor.id
                registry_id = uuid.uuid4()
                await _insert(seed, registry_id, "http://127.0.0.1:1")
                retained_key = await seed.scalar(
                    text(
                        "DELETE FROM aap_inference_backend_probe_v1.probe_secret RETURNING secret"
                    )
                )
                assert type(retained_key) is bytes and len(retained_key) == 32
            response = await httpx_client.post(
                "/api/v1/projects",
                json={
                    "name": "Missing probe key",
                    "type_label": "Image",
                    "ml_backend_source_id": str(registry_id),
                },
                headers={
                    "Authorization": "Bearer "
                    + create_access_token(str(actor.id), actor.role)
                },
            )
            assert response.status_code == 503, response.text
            async with maker() as observer:
                assert (
                    await observer.scalar(
                        text("SELECT count(*) FROM public.projects WHERE owner_id=:id"),
                        {"id": actor_id},
                    )
                    == 0
                )
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM public.project_templates WHERE created_by=:id"
                        ),
                        {"id": actor_id},
                    )
                    == 0
                )
                assert (
                    await observer.scalar(
                        text(
                            "SELECT count(*) FROM public.workspace_configuration_operation_receipts WHERE actor_id=:id"
                        ),
                        {"id": actor_id},
                    )
                    == 0
                )
        finally:
            async with maker.begin() as cleanup:
                if retained_key is not None:
                    await cleanup.execute(
                        text(
                            "INSERT INTO aap_inference_backend_probe_v1.probe_secret(singleton,secret) VALUES(true,:key)"
                        ),
                        {"key": retained_key},
                    )
                if registry_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM public.ml_backend_registry WHERE id=:id"),
                        {"id": registry_id},
                    )
                if actor_id is not None:
                    await cleanup.execute(
                        text("DELETE FROM public.users WHERE id=:id"), {"id": actor_id}
                    )
