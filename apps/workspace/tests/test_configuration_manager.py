from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from aap_inference_contract import client as backend_probe_client_module
from aap_inference_contract.capabilities import CapabilityLimits
from aap_inference_contract.probe import (
    BackendProbeInvalidRequest,
    BackendProbeLegacyFailure,
    BackendProbeUnavailable,
)
from aap_workspace.config import RuntimeSettings
from aap_workspace.configuration_contract import (
    CONFIGURATION_PROTOCOL,
    ConfigurationUnavailable,
    configuration_request,
)
from aap_workspace.configuration import (
    ConfigurationCommandFailure,
    ConfigurationManager,
)
from aap_workspace.configuration_policy import ConfigurationPolicyError

_ACTOR = UUID("6a64e81b-1ad5-4a65-8551-2798a6a1fbbd")
_PROJECT = UUID("424cc9e8-9691-4509-8d89-d936e276429b")
_TEMPLATE = UUID("09672fda-1f43-4e66-b18b-841be56b4018")
_ORG = UUID("2aa1a157-2d42-4e45-8af9-65bcf5b509df")
_CREATED = UUID("fd30b1bc-8a11-4230-b6ea-18009ee5f564")


def _actor(role="project_admin"):
    return {"id": str(_ACTOR), "role": role, "is_active": True}


def _template(**overrides):
    return {
        "id": str(_TEMPLATE),
        "created_by": str(_ACTOR),
        "source_project_id": None,
        "name": "Detection",
        "description": None,
        "type_label": "Detection",
        "type_key": "image-det",
        "data_type": "image",
        "tool_bindings": {"bbox": {"enabled": True, "classes": []}},
        "label_config": None,
        "ai_enabled": False,
        "sampling": "sequence",
        "maximum_annotations": 1,
        "show_overlap_first": False,
        "iou_dedup_threshold": 0.7,
        "box_threshold": 0.35,
        "text_threshold": 0.25,
        "rendering_config": None,
        "annotation_guide": None,
        "scope": "private",
        "organization_id": None,
        **overrides,
    }


class FakeTransaction:
    autoflush = True

    def __init__(self, *, begin=None, authority=None, failure=None):
        self.begin_facts = begin or {
            "actor": _actor(),
            "project": None,
            "source_project": None,
            "template": None,
            "replay": None,
            "is_manager": True,
        }
        self.authority_facts = authority or {
            "actor": _actor(),
            "project": None,
            "source_project": None,
            "template": None,
            "is_manager": True,
        }
        self.failure = failure
        self.calls = []
        self.caught = None
        self.flushes = 0

    async def step(self, request_json, action, args, columns, values):
        self.calls.append((action, args, columns, values))
        envelope = {
            "schema": CONFIGURATION_PROTOCOL,
            "operation_id": request_json["operation_id"],
            "operation": request_json["operation"],
            "action": action,
        }
        if self.failure is not None and action == self.failure[0]:
            return {
                **envelope,
                "ok": False,
                "error": {
                    "status_code": self.failure[1],
                    "detail": self.failure[2],
                    "headers": None,
                    "rollback": False,
                },
            }
        facts = {
            "begin": self.begin_facts,
            "authority": self.authority_facts,
            "allocate": {
                "resource_id": str(_CREATED),
                "display_id": "P-101",
            },
            "insert": {"resource_id": str(_CREATED)},
            "update": {"resource_id": str(request_json["target_id"])},
            "delete": {"resource_id": str(request_json["target_id"])},
            "finish": {
                "resource_id": str(
                    _CREATED
                    if request_json["operation"]
                    in {"create_project", "create_template", "duplicate_template"}
                    else request_json["target_id"]
                ),
                "template_id": request_json["selectors"]["template_id"]
                if request_json["operation"] == "create_project"
                else None,
            },
        }.get(action, {})
        return {**envelope, "ok": True, "facts": facts}

    async def before_project_insert(self):
        self.flushes += 1


class FakeData:
    def __init__(self, transaction):
        self.tx = transaction
        self.write = None

    @asynccontextmanager
    async def transaction(self, *, write):
        self.write = write
        try:
            yield self.tx
        except BaseException as error:
            self.tx.caught = error
            raise


_BACKEND = UUID("e2bc8042-4b7f-40e4-9860-218e68b07739")
_POOL = UUID("170742cf-96b6-4eca-9cbb-a48858eb8464")
_PROBE_SUBJECT = "v1.AAECAwQFBgcICQ=="


def _project(data_type="video"):
    return {
        "id": str(_PROJECT),
        "type_key": "video-track" if data_type == "video" else data_type,
        "data_type": data_type,
        "scene_mode": False,
        "total_tasks": 0,
        "tool_bindings": {},
        "ml_backend_pool_id": None,
    }


class BackendProbeTransaction(FakeTransaction):
    def __init__(self, backend_facts, *, data_type="video"):
        project = _project(data_type)
        super().__init__(
            begin={
                "actor": _actor(),
                "project": project,
                "source_project": None,
                "template": None,
                "replay": None,
                "is_manager": True,
            },
            authority={
                "actor": _actor(),
                "project": project,
                "source_project": None,
                "template": None,
                "is_manager": True,
            },
        )
        self.backend_facts = backend_facts

    async def step(self, request_json, action, args, columns, values):
        reply = await super().step(request_json, action, args, columns, values)
        if reply["ok"] is not True:
            return reply
        facts = dict(reply["facts"])
        if action == "backend":
            facts["backend"] = self.backend_facts
        elif action == "enable_backend":
            facts["pool_id"] = str(_POOL)
        return {**reply, "facts": facts}


def _probe_request():
    return configuration_request(
        "update_project",
        _ACTOR,
        _PROJECT,
        {"ml_backend_id": str(_BACKEND)},
    )


def _safe_backend_facts(**overrides):
    return {
        "id": str(_BACKEND),
        "probe_subject": _PROBE_SUBJECT,
        **overrides,
    }


@pytest.mark.asyncio
async def test_backend_probe_receives_only_registry_id_and_opaque_subject():
    transaction = BackendProbeTransaction(_safe_backend_facts())
    calls = []
    limits = CapabilityLimits(
        video_tracker_window_size_frames=240,
        video_tracker_sam3_window_size_frames=12,
    )

    async def backend_probe(registry_id, probe_subject, *, timeout, limits):
        calls.append((registry_id, probe_subject, timeout, limits))
        return ["image", "video"]

    manager = ConfigurationManager(
        FakeData(transaction),
        backend_probe=backend_probe,
        setup_timeout=7.5,
        capability_limits=limits,
    )
    reply = await manager.execute(_probe_request())

    assert reply.ok is True
    assert calls == [(_BACKEND, _PROBE_SUBJECT, 7.5, limits)]
    assert [call[0] for call in transaction.calls] == [
        "begin",
        "backend",
        "authority",
        "enable_backend",
        "binding",
        "update",
        "finish",
    ]
    assert "url" not in transaction.backend_facts
    assert "auth_method" not in transaction.backend_facts
    assert "auth_token" not in transaction.backend_facts
    assert "extra_params" not in transaction.backend_facts


@pytest.mark.asyncio
@pytest.mark.parametrize("modalities", [None, []])
async def test_backend_probe_null_or_empty_modalities_preserve_legacy_fail_open(
    modalities,
):
    transaction = BackendProbeTransaction(_safe_backend_facts())

    async def backend_probe(*_args, **_kwargs):
        return modalities

    reply = await ConfigurationManager(
        FakeData(transaction), backend_probe=backend_probe
    ).execute(_probe_request())

    assert reply.ok is True
    assert [call[0] for call in transaction.calls][-4:] == [
        "enable_backend",
        "binding",
        "update",
        "finish",
    ]


@pytest.mark.asyncio
async def test_backend_probe_modality_mismatch_keeps_legacy_422_phase_and_detail():
    transaction = BackendProbeTransaction(_safe_backend_facts())

    async def backend_probe(*_args, **_kwargs):
        return ["image"]

    reply = await ConfigurationManager(
        FakeData(transaction), backend_probe=backend_probe
    ).execute(_probe_request())

    assert reply.ok is False
    assert reply.error.status_code == 422
    assert reply.error.detail == (
        "该 ML Backend 不支持「video」模态 (检测到: ['image']); "
        "视频项目需绑定自报 supported_trackers 的 backend."
    )
    assert [call[0] for call in transaction.calls] == ["begin", "backend"]
    assert isinstance(transaction.caught.__cause__, ConfigurationPolicyError)
    assert transaction.caught.error.status_code == reply.error.status_code
    assert transaction.caught.error.detail == reply.error.detail


@pytest.mark.asyncio
async def test_backend_probe_provider_is_required_only_when_modalities_are_needed():
    video_transaction = BackendProbeTransaction(_safe_backend_facts())
    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(FakeData(video_transaction)).execute(
            _probe_request()
        )
    assert [call[0] for call in video_transaction.calls] == ["begin", "backend"]

    lidar_transaction = BackendProbeTransaction(
        _safe_backend_facts(), data_type="lidar"
    )
    reply = await ConfigurationManager(FakeData(lidar_transaction)).execute(
        _probe_request()
    )
    assert reply.ok is True
    assert "backend" not in [call[0] for call in lidar_transaction.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend_facts",
    [
        {
            "id": str(_BACKEND),
            "url": "https://old.example",
            "auth_token": "sentinel-secret",
        },
        _safe_backend_facts(url="https://old.example"),
        _safe_backend_facts(id="9dc1d1b0-92e0-4a38-88c2-4cfd7084420c"),
        _safe_backend_facts(probe_subject=None),
    ],
)
async def test_legacy_or_misbound_backend_facts_fail_closed(backend_facts):
    transaction = BackendProbeTransaction(backend_facts)
    calls = []

    async def backend_probe(*args, **kwargs):
        calls.append((args, kwargs))
        return ["video"]

    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(
            FakeData(transaction), backend_probe=backend_probe
        ).execute(_probe_request())
    assert calls == []
    assert [call[0] for call in transaction.calls] == ["begin", "backend"]
    if "auth_token" in backend_facts:
        assert "sentinel-secret" not in str(transaction.caught)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [BackendProbeUnavailable(), BackendProbeInvalidRequest()]
)
async def test_backend_probe_unavailable_maps_to_configuration_unavailable(failure):
    transaction = BackendProbeTransaction(_safe_backend_facts())

    async def backend_probe(*_args, **_kwargs):
        raise failure

    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(
            FakeData(transaction), backend_probe=backend_probe
        ).execute(_probe_request())
    assert transaction.caught is not None


@pytest.mark.asyncio
async def test_backend_probe_legacy_materialization_failure_is_not_hidden():
    transaction = BackendProbeTransaction(_safe_backend_facts())
    failure = BackendProbeLegacyFailure()

    async def backend_probe(*_args, **_kwargs):
        raise failure

    with pytest.raises(BackendProbeLegacyFailure) as caught:
        await ConfigurationManager(
            FakeData(transaction), backend_probe=backend_probe
        ).execute(_probe_request())
    assert caught.value is failure
    assert transaction.caught is failure


def test_unconfigured_workspace_probe_is_optional_and_does_not_reuse_control_token():
    settings = RuntimeSettings(membership_control_token="workspace-control-token")

    assert settings.inference_probe_client() is None


def test_partial_workspace_probe_configuration_is_rejected_without_fallback():
    settings = RuntimeSettings(inference_probe_url="http://inference.internal")

    with pytest.raises(ValueError, match="URL and token are required together"):
        settings.inference_probe_client()


def test_workspace_runtime_builds_probe_client_from_its_dedicated_credentials(
    monkeypatch,
):
    observed = {}

    class FakeProbeClient:
        def __init__(self, base_url, token, *, timeout_seconds):
            observed.update(
                base_url=base_url,
                token=token,
                timeout_seconds=timeout_seconds,
            )

        async def probe_modalities(self, *args, **kwargs):
            return None

    monkeypatch.setattr(
        backend_probe_client_module,
        "BackendProbeHttpClient",
        FakeProbeClient,
    )
    settings = RuntimeSettings(
        membership_control_token="workspace-control-token",
        inference_probe_url="http://inference.internal",
        inference_probe_token="inference-probe-token",
        inference_probe_timeout_seconds=12.0,
    )

    client = settings.inference_probe_client()

    assert isinstance(client, FakeProbeClient)
    assert observed == {
        "base_url": "http://inference.internal",
        "token": "inference-probe-token",
        "timeout_seconds": 12.0,
    }


@pytest.mark.asyncio
async def test_workspace_http_runtime_injects_probe_client_into_configuration_manager(
    monkeypatch,
):
    import httpx

    from aap_workspace.api import create_app

    transaction = BackendProbeTransaction(_safe_backend_facts())
    observed = []

    class FakeProbeClient:
        def __init__(self, _base_url, _token, *, timeout_seconds):
            self.timeout_seconds = timeout_seconds

        async def probe_modalities(
            self, registry_id, probe_subject, *, timeout, limits
        ):
            observed.append((registry_id, probe_subject, timeout, limits))
            return ["image", "video"]

    monkeypatch.setattr(
        backend_probe_client_module,
        "BackendProbeHttpClient",
        FakeProbeClient,
    )
    app = create_app(
        RuntimeSettings(
            membership_control_token="workspace-control-token",
            inference_probe_url="http://inference.internal",
            inference_probe_token="inference-probe-token",
        ),
        configuration_data=FakeData(transaction),
    )
    request = _probe_request()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://workspace.test"
    ) as client:
        response = await client.post(
            "/v1/workspace/configuration/execute",
            headers={"Authorization": "Bearer workspace-control-token"},
            json=request.model_dump(mode="json", by_alias=True),
        )

    assert response.status_code == 200, response.text
    assert observed and observed[0][0:3] == (_BACKEND, _PROBE_SUBJECT, 10.0)
    assert [call[0] for call in transaction.calls][-4:] == [
        "enable_backend",
        "binding",
        "update",
        "finish",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create_project", "update_project"])
async def test_missing_backend_pool_cannot_become_a_successful_or_cleared_binding(
    operation,
):
    project = {
        "id": str(_PROJECT),
        "type_key": "image-det",
        "data_type": "image",
        "scene_mode": False,
        "total_tasks": 0,
        "tool_bindings": {},
    }
    transaction = FakeTransaction(
        begin={
            "actor": _actor(),
            "project": project if operation == "update_project" else None,
            "source_project": None,
            "template": None,
            "is_manager": True,
            "replay": None,
        },
        authority={"actor": _actor(), "project": project, "is_manager": True},
    )
    backend_id = UUID("8e9f70b6-9493-4c65-b34a-2a02cc368037")
    payload = {"ml_backend_id": backend_id}
    if operation == "create_project":
        payload.update(name="Missing pool", type_label="Image")
    request = configuration_request(
        operation, _ACTOR, _PROJECT if operation == "update_project" else None, payload
    )
    with pytest.raises(ValueError, match="has no service pool"):
        await ConfigurationManager(FakeData(transaction)).execute(request)
    actions = [call[0] for call in transaction.calls]
    assert actions[-1] == "enable_backend"
    assert "binding" not in actions
    assert "finish" not in actions


@pytest.mark.asyncio
async def test_create_project_allocates_then_flushes_then_inserts_and_omits_null_defaults():
    transaction = FakeTransaction()
    manager = ConfigurationManager(FakeData(transaction))
    request = configuration_request(
        "create_project",
        _ACTOR,
        None,
        {
            "name": "New project",
            "type_label": "Detection",
            "type_key": "image-det",
            "data_type": "image",
            "box_threshold": None,
            "text_threshold": None,
        },
    )

    reply = await manager.execute(request)

    assert reply.ok is True
    assert reply.result.resource_id == _CREATED
    assert [call[0] for call in transaction.calls] == [
        "begin",
        "authority",
        "allocate",
        "insert",
        "finish",
    ]
    assert transaction.flushes == 1
    insert = transaction.calls[3]
    assert "box_threshold" not in insert[2]
    assert "text_threshold" not in insert[2]


@pytest.mark.asyncio
async def test_update_project_keeps_real_threshold_dirty_under_python_float_equality():
    project = {
        "id": str(_PROJECT),
        "is_manager": True,
        "type_key": "image-det",
        "data_type": "image",
        "scene_mode": False,
        "total_tasks": 0,
        "box_threshold": 0.3499999940395355,
        "tool_bindings": {},
    }
    transaction = FakeTransaction(
        begin={
            "actor": _actor(),
            "project": project,
            "is_manager": True,
            "replay": None,
        },
        authority={"actor": _actor(), "project": project, "is_manager": True},
    )
    manager = ConfigurationManager(FakeData(transaction))
    request = configuration_request(
        "update_project", _ACTOR, _PROJECT, {"box_threshold": 0.35}
    )

    reply = await manager.execute(request)

    assert reply.ok is True
    update = next(call for call in transaction.calls if call[0] == "update")
    assert update[2] == ["box_threshold"]
    assert update[3] == ["0.35"]


@pytest.mark.asyncio
async def test_create_template_checks_scope_before_source_authority_and_inserts():
    source = {
        "id": str(_PROJECT),
        "name": "Source",
        "type_label": "Detection",
        "type_key": "image-det",
        "data_type": "image",
        "tool_bindings": {"bbox": {"enabled": True, "classes": []}},
    }
    transaction = FakeTransaction(
        authority={
            "actor": _actor(),
            "project": None,
            "source_project": source,
            "is_manager": True,
        }
    )
    manager = ConfigurationManager(FakeData(transaction))
    request = configuration_request(
        "create_template",
        _ACTOR,
        None,
        {
            "name": "New template",
            "type_label": "Detection",
            "type_key": "image-det",
            "source_project_id": str(_PROJECT),
            "scope": "organization",
            "organization_id": str(_ORG),
        },
    )

    reply = await manager.execute(request)

    assert reply.ok is True
    assert [call[0] for call in transaction.calls] == [
        "begin",
        "authority",
        "allocate",
        "insert",
        "finish",
    ]
    insert = transaction.calls[3]
    assert "source_project_id" not in insert[2]
    assert {"name", "type_label", "type_key"}.issubset(insert[2])


@pytest.mark.asyncio
async def test_public_template_scope_rejection_precedes_source_authority():
    transaction = FakeTransaction()
    manager = ConfigurationManager(FakeData(transaction))
    request = configuration_request(
        "create_template",
        _ACTOR,
        None,
        {
            "name": "Public template",
            "type_label": "Detection",
            "type_key": "image-det",
            "source_project_id": str(_PROJECT),
            "scope": "public",
        },
    )

    reply = await manager.execute(request)

    assert reply.ok is False
    assert reply.error.status_code == 403
    assert reply.error.detail == "仅超级管理员可创建公共模板"
    assert [call[0] for call in transaction.calls] == ["begin"]
    assert isinstance(transaction.caught, ConfigurationCommandFailure)


@pytest.mark.asyncio
async def test_template_update_uses_uuid_equality_to_avoid_a_false_dirty_write():
    current = _template(scope="organization", organization_id=str(_ORG))
    transaction = FakeTransaction(
        begin={
            "actor": _actor(),
            "project": None,
            "template": current,
            "replay": None,
        }
    )
    manager = ConfigurationManager(FakeData(transaction))
    request = configuration_request(
        "update_template", _ACTOR, _TEMPLATE, {"organization_id": str(_ORG)}
    )

    reply = await manager.execute(request)

    assert reply.ok is True
    update = next(call for call in transaction.calls if call[0] == "update")
    assert update[2:] == ([], [])


@pytest.mark.asyncio
async def test_delete_and_duplicate_template_use_locked_source_and_native_write_steps():
    source = _template()
    delete_tx = FakeTransaction(
        begin={"actor": _actor(), "project": None, "template": source, "replay": None}
    )
    delete_reply = await ConfigurationManager(FakeData(delete_tx)).execute(
        configuration_request("delete_template", _ACTOR, _TEMPLATE, {})
    )
    assert delete_reply.ok is True
    assert [call[0] for call in delete_tx.calls] == ["begin", "delete", "finish"]

    duplicate_tx = FakeTransaction(
        begin={"actor": _actor(), "project": None, "template": source, "replay": None}
    )
    duplicate_reply = await ConfigurationManager(FakeData(duplicate_tx)).execute(
        configuration_request("duplicate_template", _ACTOR, _TEMPLATE, {})
    )
    assert duplicate_reply.ok is True
    assert duplicate_reply.result.resource_id == _CREATED
    assert [call[0] for call in duplicate_tx.calls] == [
        "begin",
        "allocate",
        "insert",
        "finish",
    ]


@pytest.mark.asyncio
async def test_native_failure_crosses_transaction_as_command_failure_and_is_returned():
    transaction = FakeTransaction(failure=("begin", 409, {"reason": "account_busy"}))
    manager = ConfigurationManager(FakeData(transaction))
    reply = await manager.execute(
        configuration_request("create_template", _ACTOR, None, {"name": "T"})
    )

    assert reply.ok is False
    assert reply.error.status_code == 409
    assert reply.error.detail == {"reason": "account_busy"}
    assert reply.error.rollback is False
    assert isinstance(transaction.caught, ConfigurationCommandFailure)


@pytest.mark.asyncio
async def test_replay_returns_without_parsing_payload_or_reapplying_writes():
    transaction = FakeTransaction(
        begin={
            "actor": None,
            "project": None,
            "replay": {"resource_id": str(_PROJECT), "template_id": None},
        }
    )
    manager = ConfigurationManager(FakeData(transaction))
    valid_request = configuration_request(
        "update_project", _ACTOR, _PROJECT, {"name": "Updated"}
    )
    request = valid_request.model_copy(
        update={"payload_ascii": '{"coherence":"intentionally invalid"}'}
    )

    reply = await manager.execute(request)

    assert reply.ok is True
    assert reply.result.resource_id == _PROJECT
    assert [call[0] for call in transaction.calls] == ["begin"]
