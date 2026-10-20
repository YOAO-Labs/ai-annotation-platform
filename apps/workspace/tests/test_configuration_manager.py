from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import UUID

import pytest

from aap_workspace.configuration_contract import (
    CONFIGURATION_PROTOCOL,
    configuration_request,
)
from aap_workspace.configuration import (
    ConfigurationCommandFailure,
    ConfigurationManager,
)

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
