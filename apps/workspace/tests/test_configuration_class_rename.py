from __future__ import annotations

from contextlib import asynccontextmanager
import json
from importlib.resources import files
from types import SimpleNamespace
import uuid

import pytest

from aap_workspace.configuration import ConfigurationManager
from aap_workspace.configuration_contract import (
    CONFIGURATION_PROTOCOL,
    ConfigurationFailure,
    ConfigurationRenameSuccess,
    ConfigurationUnavailable,
    configuration_request,
    configuration_schema,
    configuration_rename_success_reply,
    decode_configuration_reply,
)
from aap_workspace.project_queries import ProjectQueryManager
from aap_workspace_contract.project_query_contract import (
    PROJECT_JSON_COLUMNS,
    PROJECT_QUERY_PROTOCOL,
    PROJECT_ROW_COLUMNS,
    ProjectClassRenameEvidence,
    ProjectQueryFailure,
    ProjectQueryUnavailable,
)

ACTOR = uuid.UUID(int=1)
PROJECT = uuid.UUID(int=2)
_OPERATION_ID = uuid.UUID("45ea813a-1324-44df-9b1b-4a139cf5b6e4")
_OTHER_ACTOR = uuid.UUID("6fbe910d-fd82-41b2-b887-ef4c6dde9e72")
_MEMBER_ID = uuid.UUID("5c36b923-6419-41eb-926f-88fb07425bf6")
_NOW = "2026-01-02T00:00:00+00:00"


def test_configuration_schema_adds_rename_without_widening_legacy_success():
    schema = configuration_schema()
    stored = json.loads(
        files("aap_workspace")
        .joinpath("configuration_protocol.schema.json")
        .read_text(encoding="utf-8")
    )

    assert stored == schema
    assert (
        "rename_project_class"
        in schema["$defs"]["ConfigurationRequest"]["properties"]["operation"]["enum"]
    )
    assert (
        "rename_project_class"
        not in schema["$defs"]["ConfigurationSuccess"]["properties"]["operation"][
            "enum"
        ]
    )
    assert schema["$defs"]["ConfigurationRenameResult"]["required"] == [
        "resource_id",
        "changed",
        "evidence",
        "projection",
    ]


def _project_row(**changes):
    value = dict.fromkeys(PROJECT_ROW_COLUMNS)
    value.update({field: "{}" for field in PROJECT_JSON_COLUMNS})
    value.update(
        id=str(PROJECT),
        owner_id=str(ACTOR),
        display_id="P-LOCAL",
        name="Project",
        type_label="Image",
        type_key="image-det",
        data_type="image",
        status="in_progress",
        sampling="sequence",
        maximum_annotations=1,
        total_tasks=3,
        completed_tasks=1,
        review_tasks=1,
        in_progress_tasks=1,
        task_lock_ttl_seconds=300,
        iou_dedup_threshold="0.7",
        box_threshold="0.35",
        text_threshold="0.25",
        ai_enabled=False,
        show_overlap_first=False,
        ai_interactive_enabled=True,
        raster_mask_native_editing_enabled=True,
        scene_mode=False,
        prefer_same_scene_continuation=False,
        scene_continuation_window_min=30,
        due_date="2026-01-02",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-02T00:00:00+00:00",
        guide_assets="[]",
        preannotate_pipeline=None,
        annotation_guide="Guide",
        tool_bindings=json.dumps(_bindings("Car"), ensure_ascii=True),
    )
    return {**value, **changes}


def _serialization_facts(value):
    return {
        "projection_row": {"row_json": json.dumps(value)},
        "ai_counts": {"counts": [{"project_id": str(PROJECT), "count": "1"}]},
        "member_count": {"count": "1"},
        "pool": {"legacy_instance_id": None},
        "my_roles": {"memberships": []},
    }


class ProjectQueryData:
    def __init__(self, facts):
        self.facts = facts
        self.events = []

    @asynccontextmanager
    async def transaction(self):
        self.events.append("transaction")
        yield self

    async def step(self, request, action, parameters=None, filters=None):
        self.events.append((action, parameters))
        facts = self.facts[action]
        if callable(facts):
            facts = facts(parameters)
        elif isinstance(facts, list):
            facts = facts.pop(0)
        return {
            "schema": PROJECT_QUERY_PROTOCOL,
            "request": request.model_dump(mode="json", by_alias=True),
            "action": action,
            "parameters": parameters,
            "facts": facts,
        }


class ProjectQueryProfiles:
    def __init__(self, events):
        self.events = events

    async def resolve(self, ids):
        self.events.append(("profiles", set(ids)))
        return {
            str(ACTOR): SimpleNamespace(
                id=ACTOR,
                name="Owner",
                email="owner@example.test",
                role="project_admin",
                avatar_initial="O",
                avatar_ref=None,
            )
        }


def _bindings(*names: str) -> dict:
    return {
        "bbox": {
            "enabled": True,
            "classes": [
                {"name": name, "order": index} for index, name in enumerate(names)
            ],
            "attribute_schema": {"fields": []},
        }
    }


def _request(old="Car", new="Truck", *, unit="bbox", operation_id=_OPERATION_ID):
    return configuration_request(
        "rename_project_class",
        ACTOR,
        PROJECT,
        {"old_name": old, "new_name": new, "tool_unit_id": unit},
        operation_id=operation_id,
    )


def _evidence(operation_id=_OPERATION_ID):
    return ProjectClassRenameEvidence(
        source="class_rename",
        operation_id=operation_id,
        source_operation="rename_class",
        actor_id=ACTOR,
        project_id=PROJECT,
        owner_id=ACTOR,
        platform_role="project_admin",
        is_active=True,
    )


def _member_row(*, actor_id=ACTOR, project_id=PROJECT, role="viewer") -> dict:
    return {
        "id": str(_MEMBER_ID),
        "project_id": str(project_id),
        "user_id": str(actor_id),
        "role": role,
        "assigned_by": str(_OTHER_ACTOR),
        "assigned_at": _NOW,
        "version": 4,
        "updated_at": _NOW,
        "weekly_target": None,
    }


class RenameTransaction:
    autoflush = True

    def __init__(
        self, *, role="project_admin", owner_id=ACTOR, bindings=None, member_rows=None
    ):
        self.rollback_on_exit = False
        self.events = []
        self.role = role
        self.owner_id = owner_id
        self.bindings = bindings if bindings is not None else _bindings("Car")
        self.member_rows = member_rows if member_rows is not None else []
        self.failed_action = None
        self.failure = None

    def _row_json(self, *, bindings=None, owner_id=None):
        return json.dumps(
            _project_row(
                id=str(PROJECT),
                owner_id=(
                    str(self.owner_id if owner_id is None else owner_id)
                    if (self.owner_id if owner_id is None else owner_id) is not None
                    else None
                ),
                tool_bindings=json.dumps(
                    self.bindings if bindings is None else bindings,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
        )

    async def step(self, request_json, action, args, columns, values):
        self.events.append((action, args, columns, values))
        envelope = {
            "schema": CONFIGURATION_PROTOCOL,
            "operation_id": request_json["operation_id"],
            "operation": request_json["operation"],
            "action": action,
        }
        if action == self.failed_action:
            status_code, detail = self.failure
            return {
                **envelope,
                "ok": False,
                "error": {
                    "status_code": status_code,
                    "detail": detail,
                    "headers": None,
                    "rollback": False,
                },
            }
        facts = {
            "class_rename_prepare": {
                "actor": {
                    "actor_id": str(ACTOR),
                    "platform_role": self.role,
                    "is_active": True,
                },
                "row_json": self._row_json(),
            },
            "class_rename_member": {"rows_json": json.dumps(self.member_rows)},
            "class_rename_lock": {"row_json": self._row_json()},
            "class_rename_apply": {},
        }.get(action, {})
        return {**envelope, "ok": True, "facts": facts}

    async def before_project_insert(self):
        raise AssertionError("class rename must not use the generic insert flush hook")


class RenameData:
    def __init__(self, transaction, *, independent=False):
        self.tx = transaction
        self.independent = independent
        self.exit = None

    @asynccontextmanager
    async def transaction(self, *, write):
        assert write is True
        try:
            yield self.tx
        except BaseException:
            self.exit = "exception_rollback"
            raise
        else:
            self.exit = "rollback" if self.tx.rollback_on_exit else "normal_exit"


def _events(transaction):
    return [event[0] for event in transaction.events]


@pytest.mark.asyncio
async def test_changed_rename_keeps_exact_native_order_and_narrow_apply_arguments():
    transaction = RenameTransaction()
    data = RenameData(transaction)

    async def unexpected_projection(_request):
        raise AssertionError("changed rename does not project inside its owner tx")

    reply = await ConfigurationManager(
        data, noop_projection=unexpected_projection
    ).execute(_request())
    assert isinstance(reply, ConfigurationRenameSuccess)
    assert reply.result.changed is True
    assert reply.result.projection is None
    assert _events(transaction) == [
        "class_rename_prepare",
        "class_rename_lock",
        "class_rename_apply",
    ]
    assert all(call[2:] == ([], []) for call in transaction.events)
    apply_args = transaction.events[-1][1]
    assert set(apply_args) == {"old_name", "new_name", "tool_bindings_ascii"}
    assert apply_args["old_name"] == "Car"
    assert apply_args["new_name"] == "Truck"
    assert json.loads(apply_args["tool_bindings_ascii"]) == _bindings("Truck")
    assert data.exit == "normal_exit"


@pytest.mark.asyncio
async def test_borrowed_noop_skips_binding_policy_and_keeps_caller_transaction():
    transaction = RenameTransaction()
    data = RenameData(transaction)
    reply = await ConfigurationManager(data).execute(
        _request("Missing", " Missing ", unit="not-a-unit")
    )

    assert isinstance(reply, ConfigurationRenameSuccess)
    assert reply.result.changed is False
    assert reply.result.projection is None
    assert _events(transaction) == ["class_rename_prepare"]
    assert transaction.rollback_on_exit is False
    assert data.exit == "normal_exit"


@pytest.mark.asyncio
async def test_member_rows_are_decoded_before_visible_nonmanager_denial():
    transaction = RenameTransaction(
        role="project_admin",
        owner_id=_OTHER_ACTOR,
        member_rows=[_member_row()],
    )
    data = RenameData(transaction)
    reply = await ConfigurationManager(data).execute(_request())

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 403
    assert _events(transaction) == [
        "class_rename_prepare",
        "class_rename_member",
    ]


@pytest.mark.asyncio
async def test_missing_member_stays_hidden_before_any_exclusive_project_lock():
    transaction = RenameTransaction(role="employee", owner_id=_OTHER_ACTOR)
    data = RenameData(transaction)
    reply = await ConfigurationManager(data).execute(_request())

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 404
    assert _events(transaction) == [
        "class_rename_prepare",
        "class_rename_member",
    ]


@pytest.mark.asyncio
async def test_malformed_member_snapshot_fails_before_visibility_policy():
    member = _member_row()
    member.pop("updated_at")
    transaction = RenameTransaction(
        role="project_admin",
        owner_id=_OTHER_ACTOR,
        member_rows=[member],
    )

    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(RenameData(transaction)).execute(_request())

    assert _events(transaction) == [
        "class_rename_prepare",
        "class_rename_member",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_name", "new_name", "unit", "bindings", "status_code"),
    (
        ("Car", "Truck", "bbox", _bindings("Car", "Truck"), 409),
        ("Car", "Truck", "unknown", _bindings("Car"), 404),
        ("Missing", "Truck", None, _bindings("Car"), 404),
    ),
)
async def test_changed_policy_errors_happen_after_refresh_but_before_apply(
    old_name, new_name, unit, bindings, status_code
):
    transaction = RenameTransaction(bindings=bindings)
    data = RenameData(transaction)
    reply = await ConfigurationManager(data).execute(
        _request(old_name, new_name, unit=unit)
    )

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == status_code
    assert _events(transaction) == ["class_rename_prepare", "class_rename_lock"]


@pytest.mark.asyncio
async def test_independent_noop_binds_full_query_reply_and_marks_rollback():
    transaction = RenameTransaction()
    data = RenameData(transaction, independent=True)
    callback_requests = []

    async def noop_projection(project_request):
        callback_requests.append(project_request)
        query_data = ProjectQueryData(_serialization_facts(_project_row()))
        return await ProjectQueryManager(
            query_data, ProjectQueryProfiles(query_data.events)
        ).execute(project_request)

    request = _request("Car", "Car", unit="unknown")
    reply = await ConfigurationManager(data, noop_projection=noop_projection).execute(
        request
    )

    assert isinstance(reply, ConfigurationRenameSuccess)
    assert reply.result.changed is False
    assert reply.result.projection is not None
    assert _events(transaction) == ["class_rename_prepare"]
    assert transaction.rollback_on_exit is True
    assert data.exit == "rollback"
    assert len(callback_requests) == 1
    projection_request = callback_requests[0]
    assert projection_request.request_id == _OPERATION_ID
    assert projection_request.operation == "get_project_projection"
    assert projection_request.admitted_actor.id == ACTOR
    assert projection_request.admitted_actor.role == "project_admin"
    assert projection_request.target_id == PROJECT
    assert projection_request.filters_ascii is None
    assert projection_request.pagination is None
    assert projection_request.projection_evidence == reply.result.evidence
    assert decode_configuration_reply(request, reply) == reply

    tampered = reply.model_dump(mode="json", by_alias=True)
    tampered["result"]["projection"]["request"]["request_id"] = str(uuid.uuid4())
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(request, tampered)


@pytest.mark.asyncio
async def test_independent_noop_fails_closed_without_projection_capability():
    transaction = RenameTransaction()
    data = RenameData(transaction, independent=True)

    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(data).execute(_request("Car", "Car"))

    assert _events(transaction) == ["class_rename_prepare"]
    assert transaction.rollback_on_exit is False
    assert data.exit == "exception_rollback"


@pytest.mark.asyncio
async def test_independent_noop_projection_failure_rolls_back_without_fallback():
    transaction = RenameTransaction()
    data = RenameData(transaction, independent=True)

    async def failed_projection(_request):
        raise ProjectQueryUnavailable()

    reply = await ConfigurationManager(data, noop_projection=failed_projection).execute(
        _request("Car", "Car")
    )

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 503
    assert reply.error.detail == "项目查询服务暂不可用"
    assert _events(transaction) == ["class_rename_prepare"]
    assert transaction.rollback_on_exit is False
    assert data.exit == "exception_rollback"


@pytest.mark.asyncio
async def test_rename_step_failure_preserves_native_busy_error():
    transaction = RenameTransaction()
    transaction.failed_action = "class_rename_lock"
    transaction.failure = (409, {"reason": "project_busy"})
    reply = await ConfigurationManager(RenameData(transaction)).execute(_request())

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 409
    assert reply.error.detail == {"reason": "project_busy"}
    assert reply.error.rollback is False
    assert _events(transaction) == ["class_rename_prepare", "class_rename_lock"]


@pytest.mark.asyncio
async def test_malformed_payload_and_missing_independence_marker_fail_before_native_io():
    transaction = RenameTransaction()
    data = RenameData(transaction)
    del data.independent
    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(data).execute(_request())
    assert transaction.events == []

    data = RenameData(transaction)
    request = configuration_request(
        "rename_project_class",
        ACTOR,
        PROJECT,
        {"old_name": "Car", "new_name": "Truck", "tool_unit_id": "bbox"},
        operation_id=_OPERATION_ID,
    )
    malformed = request.model_copy(update={"payload_ascii": "not-json"})
    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(data).execute(malformed)
    assert transaction.events == []


@pytest.mark.asyncio
async def test_null_names_keep_the_legacy_400_before_any_lock():
    for old_name, new_name in (("  ", "Truck"), ("Car", "\t")):
        transaction = RenameTransaction()
        data = RenameData(transaction)
        reply = await ConfigurationManager(data).execute(_request(old_name, new_name))
        assert isinstance(reply, ConfigurationFailure)
        assert reply.error.status_code == 400
        assert transaction.events == []
        assert data.exit is None


class _FakePgTransaction:
    def __init__(self, events):
        self.events = events
        self.active = True

    async def __aenter__(self):
        self.events.append("begin")
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if self.active:
            self.events.append("rollback" if exc_type else "commit")
            self.active = False

    async def rollback(self):
        raise RuntimeError(
            "managed asyncpg transactions cannot be manually rolled back"
        )


class _FakePgConnection:
    def __init__(self):
        self.events = []
        self.terminated = False

    def transaction(self):
        return _FakePgTransaction(self.events)

    async def close(self, **_kwargs):
        self.events.append("close")

    def terminate(self):
        self.terminated = True


@pytest.mark.asyncio
async def test_standalone_configuration_data_honors_explicit_noop_rollback_marker(
    monkeypatch,
):
    from aap_workspace.configuration_postgres import ConfigurationPostgresData

    data = ConfigurationPostgresData("postgresql://private/test")
    connection = _FakePgConnection()

    async def connect():
        return connection

    async def verify_policy(_connection):
        return None

    monkeypatch.setattr(data, "_connect", connect)
    monkeypatch.setattr(data, "_verify_policy", verify_policy)

    async with data.transaction(write=True) as transaction:
        assert data.independent is True
        assert transaction.rollback_on_exit is False
        transaction.rollback_on_exit = True

    assert connection.events == ["begin", "rollback", "close"]


@pytest.mark.asyncio
async def test_noop_callback_typed_query_failure_preserves_its_public_error():
    transaction = RenameTransaction()
    data = RenameData(transaction, independent=True)

    async def failed_projection(project_request):
        return ProjectQueryFailure(
            schema="aap-workspace-project-query/v1",
            ok=False,
            request=project_request,
            error={
                "status_code": 503,
                "detail": "身份资料服务暂不可用",
                "headers": None,
            },
        )

    reply = await ConfigurationManager(data, noop_projection=failed_projection).execute(
        _request("Car", "Car")
    )

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 503
    assert reply.error.detail == "身份资料服务暂不可用"
    assert reply.error.headers is None
    assert data.exit == "exception_rollback"
    assert transaction.rollback_on_exit is False


@pytest.mark.asyncio
async def test_noop_callback_transport_unavailability_uses_project_query_503():
    transaction = RenameTransaction()
    data = RenameData(transaction, independent=True)

    async def unavailable_projection(_project_request):
        raise ProjectQueryUnavailable()

    reply = await ConfigurationManager(
        data, noop_projection=unavailable_projection
    ).execute(_request("Car", "Car"))

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 503
    assert reply.error.detail == "项目查询服务暂不可用"
    assert data.exit == "exception_rollback"


@pytest.mark.asyncio
async def test_noop_callback_unexpected_owner_500_remains_a_plain_error():
    transaction = RenameTransaction()
    data = RenameData(transaction, independent=True)

    async def internal_error(_project_request):
        raise RuntimeError("Project query owner failed")

    with pytest.raises(RuntimeError, match="Project query owner failed"):
        await ConfigurationManager(data, noop_projection=internal_error).execute(
            _request("Car", "Car")
        )

    assert data.exit == "exception_rollback"
    assert transaction.rollback_on_exit is False


@pytest.mark.parametrize(
    ("old_name", "new_name", "claimed_changed"),
    (("Car", "Truck", False), ("Car", " Car ", True)),
)
def test_rename_success_changed_flag_must_match_the_canonical_request_names(
    old_name, new_name, claimed_changed
):
    request = _request(old_name, new_name)
    reply = configuration_rename_success_reply(
        request,
        resource_id=PROJECT,
        changed=claimed_changed,
        evidence=_evidence(),
        projection=None,
    )

    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(request, reply)


@pytest.mark.asyncio
async def test_postgres_adapter_does_not_swallow_unrelated_core_errors(monkeypatch):
    from aap_workspace.configuration_postgres import ConfigurationPostgresData

    data = ConfigurationPostgresData("postgresql://private/test")
    connection = _FakePgConnection()

    async def connect():
        return connection

    async def verify_policy(_connection):
        return None

    monkeypatch.setattr(data, "_connect", connect)
    monkeypatch.setattr(data, "_verify_policy", verify_policy)

    with pytest.raises(ValueError, match="plain core error"):
        async with data.transaction(write=True):
            raise ValueError("plain core error")

    assert connection.events == ["begin", "rollback", "close"]
