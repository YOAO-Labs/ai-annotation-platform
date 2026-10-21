from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
import json
from importlib.resources import files
import uuid

import pytest

from aap_workspace.configuration import (
    ConfigurationManager,
    ConfigurationPostCommitFailure,
)
from aap_workspace.configuration_contract import (
    CONFIGURATION_PROTOCOL,
    GUIDE_OPERATIONS,
    ConfigurationFailure,
    ConfigurationGuideDeleteData,
    ConfigurationGuideSignData,
    ConfigurationGuideSuccess,
    ConfigurationGuideUploadCompleteData,
    ConfigurationGuideUploadInitData,
    ConfigurationUnavailable,
    configuration_guide_data,
    configuration_guide_payload,
    configuration_guide_success_reply,
    configuration_request,
    configuration_schema,
    decode_configuration_reply,
)
from aap_workspace.guide_asset_policy import MAX_GUIDE_ASSET_SIZE_BYTES
from aap_workspace_contract.project_query_contract import (
    PROJECT_JSON_COLUMNS,
    PROJECT_ROW_COLUMNS,
)

ACTOR = uuid.UUID("7ff7937d-6c84-4e97-9aa9-d51c79956225")
OTHER_ACTOR = uuid.UUID("e8c3108e-589d-4785-94f3-d5e9d10915ec")
PROJECT = uuid.UUID("5ab9e0d2-6625-41d8-b17c-7fe5219521c5")
ASSET_ID = uuid.UUID("d7f1cb0a-b9c6-4a7d-a2cc-0fe335b7d1f1")
OTHER_ASSET_ID = uuid.UUID("75a0b09e-f951-4467-a80b-560de57f8365")
KEY = f"projects/{PROJECT}/guide/{ASSET_ID}-guide.png"
OTHER_KEY = f"projects/{PROJECT}/guide/{OTHER_ASSET_ID}-other.png"
OPERATION_ID = uuid.UUID("107f1a30-b090-48ce-a1c4-bfb7281599da")
NOW = "2026-10-21T01:02:03+00:00"


def _project_row(guide_assets=None, *, owner_id=ACTOR):
    row = {column: None for column in PROJECT_ROW_COLUMNS}
    for column in PROJECT_JSON_COLUMNS:
        row[column] = "{}"
    row.update(
        id=str(PROJECT),
        owner_id=str(owner_id) if owner_id is not None else None,
        guide_assets=json.dumps([] if guide_assets is None else guide_assets),
        tool_bindings="{}",
        created_at=NOW,
        updated_at=NOW,
    )
    return row


def _member(*, role="reviewer", assigned_at=NOW, updated_at=NOW):
    return {
        "id": str(uuid.UUID(int=201)),
        "project_id": str(PROJECT),
        "user_id": str(ACTOR),
        "role": role,
        "assigned_by": str(OTHER_ACTOR),
        "assigned_at": assigned_at,
        "version": 3,
        "updated_at": updated_at,
        "weekly_target": None,
    }


def _actor(*, role="super_admin", active=True, actor_id=ACTOR):
    return {
        "actor_id": str(actor_id),
        "platform_role": role,
        "is_active": active,
    }


def _request(operation, payload, *, actor_id=ACTOR, target_id=PROJECT):
    return configuration_request(
        operation,
        actor_id,
        target_id,
        payload,
        operation_id=OPERATION_ID,
    )


class GuideTransaction:
    autoflush = True

    def __init__(self, data):
        self.data = data
        self.rollback_on_exit = False
        self.start_assets = deepcopy(data.guide_assets)

    async def step(self, request_json, action, args, columns, values):
        assert not columns
        assert not values
        self.data.events.append(("step", action, args))
        if action == self.data.failed_action:
            return {
                "schema": CONFIGURATION_PROTOCOL,
                "operation_id": request_json["operation_id"],
                "operation": request_json["operation"],
                "action": action,
                "ok": False,
                "error": {
                    "status_code": 409,
                    "detail": {"reason": "project_busy"},
                    "headers": None,
                    "rollback": False,
                },
            }
        actor = self.data.actor
        row_json = json.dumps(
            _project_row(self.data.guide_assets, owner_id=self.data.owner_id)
        )
        if action == "guide_account_exists":
            facts = {"exists": self.data.account_exists}
        elif action == "guide_read_project":
            facts = {"row_json": row_json}
        elif action == "guide_actor_status":
            facts = {"actor": actor if self.data.actor_exists else None}
        elif action == "guide_read_member":
            facts = {"rows_json": json.dumps(self.data.member_rows)}
        elif action == "guide_shared":
            facts = {
                "actor": actor if self.data.actor_exists else None,
                "row_json": row_json,
            }
        elif action == "guide_member":
            facts = {"rows_json": json.dumps(self.data.member_rows)}
        elif action == "guide_lock":
            facts = {"row_json": row_json}
        elif action == "guide_apply":
            assert set(args) == {"guide_assets_ascii"}
            self.data.guide_assets = json.loads(args["guide_assets_ascii"])
            facts = {}
        elif action == "guide_refresh":
            facts = {"row_json": row_json}
        else:
            raise AssertionError(f"unexpected guide action {action}")
        return {
            "schema": CONFIGURATION_PROTOCOL,
            "operation_id": request_json["operation_id"],
            "operation": request_json["operation"],
            "action": action,
            "ok": True,
            "facts": facts,
        }

    async def before_project_insert(self):
        raise AssertionError("guide operations do not use project insert")


class GuideData:
    def __init__(
        self,
        *,
        independent=False,
        guide_assets=None,
        owner_id=ACTOR,
        actor_role="super_admin",
        member_rows=None,
        account_exists=True,
        actor_exists=True,
        failed_action=None,
    ):
        self.independent = independent
        self.guide_assets = deepcopy(guide_assets or [])
        self.owner_id = owner_id
        self.actor = _actor(role=actor_role, actor_id=ACTOR)
        self.member_rows = member_rows if member_rows is not None else []
        self.account_exists = account_exists
        self.actor_exists = actor_exists
        self.failed_action = failed_action
        self.events = []
        self.transactions = []

    @asynccontextmanager
    async def transaction(self, *, write):
        assert write is True
        transaction = GuideTransaction(self)
        self.transactions.append(transaction)
        self.events.append(("transaction_start", len(self.transactions)))
        try:
            yield transaction
        except BaseException:
            self.guide_assets = transaction.start_assets
            self.events.append(("transaction_exception", len(self.transactions)))
            raise
        else:
            if transaction.rollback_on_exit:
                self.guide_assets = transaction.start_assets
                status = "rollback"
            else:
                status = "commit" if self.independent else "borrowed_exit"
            self.events.append(("transaction_exit", len(self.transactions), status))


class GuideObjects:
    def __init__(self, events, *, head_size=32, delete_error=None):
        self.events = events
        self.head_size = head_size
        self.delete_error = delete_error

    async def generate_upload_url(self, project_id, key, *, content_type):
        self.events.append(("put_url", project_id, key, content_type))
        return "https://signed.example.test/put?sig=upload"

    async def verify_upload(self, project_id, key):
        self.events.append(("head", project_id, key))
        return self.head_size

    async def delete_object(self, project_id, key):
        self.events.append(("object_delete", project_id, key))
        if self.delete_error is not None:
            raise self.delete_error

    async def generate_download_url(self, project_id, key):
        self.events.append(("get_url", project_id, key))
        return "https://signed.example.test/get?sig=download"


def _action_names(data):
    return [event[1] for event in data.events if event[0] == "step"]


@pytest.mark.asyncio
async def test_init_uses_shared_manager_then_policy_then_presign_and_rolls_back_independent():
    data = GuideData(independent=True)
    objects = GuideObjects(data.events)
    request = _request(
        "guide_asset_upload_init",
        {"filename": "folder\\图像.png", "content_type": "image/png", "size": 16},
    )
    reply = await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert isinstance(reply, ConfigurationGuideSuccess)
    assert reply.result.resource_id == PROJECT
    assert reply.result.refreshed is False
    assert reply.result.data_json.isascii()
    result = configuration_guide_data(reply)
    assert isinstance(result, ConfigurationGuideUploadInitData)
    assert result.key.startswith(f"projects/{PROJECT}/guide/")
    assert result.key.endswith("-图像.png")
    assert result.expires_in == 900
    assert _action_names(data) == ["guide_shared"]
    assert data.events.index(
        ("put_url", PROJECT, result.key, "image/png")
    ) < data.events.index(("transaction_exit", 1, "rollback"))
    assert data.transactions[0].rollback_on_exit is True
    assert data.events[-1] == ("transaction_exit", 1, "rollback")


@pytest.mark.asyncio
async def test_init_mime_precedes_size_and_missing_port_fails_only_at_effect_phase():
    data = GuideData()
    request = _request(
        "guide_asset_upload_init",
        {
            "filename": "x.exe",
            "content_type": "application/x-msdownload",
            "size": MAX_GUIDE_ASSET_SIZE_BYTES + 1,
        },
    )
    reply = await ConfigurationManager(data).execute(request)

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 400
    assert reply.error.detail == ("Unsupported content type: application/x-msdownload")
    assert _action_names(data) == ["guide_shared"]
    assert data.events[-1] == ("transaction_exception", 1)


@pytest.mark.asyncio
async def test_owner_facts_cannot_escape_the_requested_actor_scope():
    data = GuideData()
    request = _request(
        "guide_asset_upload_init",
        {"filename": "x.png", "content_type": "image/png", "size": 1},
        actor_id=OTHER_ACTOR,
    )
    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(
            data, guide_objects=GuideObjects(data.events)
        ).execute(request)
    assert _action_names(data) == ["guide_shared"]
    assert not any(event[0] == "put_url" for event in data.events)

    data = GuideData()
    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(data).execute(
            _request(
                "guide_asset_upload_init",
                {"filename": "x.png", "content_type": "image/png", "size": 1},
            )
        )
    assert _action_names(data) == ["guide_shared"]
    assert data.events[-1] == ("transaction_exception", 1)


@pytest.mark.asyncio
async def test_complete_preserves_legacy_phase_order_and_binds_typed_entry():
    data = GuideData(independent=True)
    objects = GuideObjects(data.events, head_size=2048)
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": "folder\\guide.png", "content_type": "image/png"},
    )
    reply = await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert isinstance(reply, ConfigurationGuideSuccess)
    assert reply.result.refreshed is True
    result = configuration_guide_data(reply)
    assert isinstance(result, ConfigurationGuideUploadCompleteData)
    assert result.key == KEY
    assert result.original_name == "guide.png"
    assert result.content_type == "image/png"
    assert result.size == 2048
    assert result.uploaded_at.endswith("+00:00")
    assert [event for event in data.events if event[0] == "transaction_exit"] == [
        ("transaction_exit", 1, "commit"),
        ("transaction_exit", 2, "rollback"),
    ]
    assert _action_names(data) == [
        "guide_account_exists",
        "guide_read_project",
        "guide_actor_status",
        "guide_read_project",
        "guide_shared",
        "guide_lock",
        "guide_apply",
        "guide_refresh",
    ]
    head_index = data.events.index(("head", PROJECT, KEY))
    shared_index = next(
        i
        for i, event in enumerate(data.events)
        if event[:2] == ("step", "guide_shared")
    )
    lock_index = next(
        i for i, event in enumerate(data.events) if event[:2] == ("step", "guide_lock")
    )
    apply_index = next(
        i for i, event in enumerate(data.events) if event[:2] == ("step", "guide_apply")
    )
    assert head_index < shared_index < lock_index < apply_index
    assert data.guide_assets[0]["key"] == KEY


@pytest.mark.asyncio
async def test_borrowed_complete_leaves_commit_and_full_refresh_to_the_caller():
    data = GuideData(independent=False)
    objects = GuideObjects(data.events, head_size=9)
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": "guide.png", "content_type": "image/png"},
    )
    manager = ConfigurationManager(data, guide_objects=objects)
    reply = await manager.execute(request)

    assert isinstance(reply, ConfigurationGuideSuccess)
    assert reply.result.refreshed is False
    assert [event for event in data.events if event[0] == "transaction_exit"] == [
        ("transaction_exit", 1, "borrowed_exit")
    ]
    refreshed = await manager.refresh_guide(request)
    assert refreshed["id"] == str(PROJECT)
    assert _action_names(data)[-1] == "guide_refresh"
    assert [event for event in data.events if event[0] == "transaction_exit"][-1] == (
        "transaction_exit",
        2,
        "borrowed_exit",
    )


@pytest.mark.asyncio
async def test_complete_missing_head_object_precedes_shared_write_lock():
    data = GuideData()
    objects = GuideObjects(data.events, head_size=None)
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": "guide.png", "content_type": "image/png"},
    )
    reply = await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 400
    assert reply.error.detail == "File not found in storage"
    assert _action_names(data) == [
        "guide_account_exists",
        "guide_read_project",
        "guide_actor_status",
        "guide_read_project",
    ]
    assert any(event[0] == "head" for event in data.events)
    assert "guide_shared" not in _action_names(data)
    assert "guide_lock" not in _action_names(data)


@pytest.mark.asyncio
async def test_complete_key_scope_validation_precedes_mime_and_head():
    data = GuideData()
    objects = GuideObjects(data.events)
    request = _request(
        "guide_asset_upload_complete",
        {
            "key": "foreign/key",
            "original_name": "guide.png",
            "content_type": "application/x-msdownload",
        },
    )
    reply = await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 404
    assert reply.error.detail == "Guide asset not found"
    assert _action_names(data) == [
        "guide_account_exists",
        "guide_read_project",
        "guide_actor_status",
        "guide_read_project",
    ]
    assert not any(event[0] == "head" for event in data.events)


@pytest.mark.asyncio
async def test_invalid_head_size_cleanup_failure_is_plain_error_without_metadata_apply():
    data = GuideData()
    objects = GuideObjects(
        data.events, head_size=0, delete_error=RuntimeError("delete failed")
    )
    original = deepcopy(data.guide_assets)
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": "guide.png", "content_type": "image/png"},
    )

    with pytest.raises(RuntimeError, match="delete failed"):
        await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert any(event[0] == "object_delete" for event in data.events)
    assert "guide_apply" not in _action_names(data)
    assert data.guide_assets == original


@pytest.mark.asyncio
async def test_native_project_busy_failure_stops_before_asset_metadata_apply():
    data = GuideData(failed_action="guide_lock")
    objects = GuideObjects(data.events, head_size=8)
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": "guide.png", "content_type": "image/png"},
    )

    reply = await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 409
    assert reply.error.detail == {"reason": "project_busy"}
    assert reply.error.rollback is False
    assert "guide_lock" in _action_names(data)
    assert "guide_apply" not in _action_names(data)


@pytest.mark.asyncio
async def test_complete_raw_business_value_errors_remain_late_python_errors():
    data = GuideData()
    objects = GuideObjects(data.events, head_size=3)
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": 7, "content_type": "image/png"},
    )

    with pytest.raises(AttributeError):
        await ConfigurationManager(data, guide_objects=objects).execute(request)
    assert "head" in [event[0] for event in data.events]
    assert "guide_lock" in _action_names(data)
    assert "guide_apply" not in _action_names(data)


@pytest.mark.asyncio
async def test_malformed_stored_guide_entry_fails_after_project_lock_before_storage_delete():
    data = GuideData(guide_assets=[None])
    objects = GuideObjects(data.events)
    request = _request("guide_asset_delete", {"key": KEY})

    with pytest.raises(AttributeError):
        await ConfigurationManager(data, guide_objects=objects).execute(request)
    assert _action_names(data) == ["guide_shared", "guide_lock"]
    assert not any(event[0] == "object_delete" for event in data.events)
    assert "guide_apply" not in _action_names(data)


@pytest.mark.asyncio
async def test_delete_removes_every_registered_duplicate_even_when_byte_delete_fails():
    data = GuideData(
        guide_assets=[
            {"key": KEY, "order": 1},
            {"key": OTHER_KEY, "order": 2},
            {"key": KEY, "order": 3},
        ]
    )
    objects = GuideObjects(data.events, delete_error=RuntimeError("storage offline"))
    request = _request("guide_asset_delete", {"key": KEY})
    reply = await ConfigurationManager(data, guide_objects=objects).execute(request)

    assert isinstance(reply, ConfigurationGuideSuccess)
    assert reply.result.refreshed is False
    result = configuration_guide_data(reply)
    assert isinstance(result, ConfigurationGuideDeleteData)
    assert result.deleted == KEY
    assert data.guide_assets == [{"key": OTHER_KEY, "order": 2}]
    assert _action_names(data) == ["guide_shared", "guide_lock", "guide_apply"]
    assert any(event[0] == "object_delete" for event in data.events)


@pytest.mark.asyncio
async def test_sign_uses_locked_visibility_then_membership_before_registration_and_get():
    data = GuideData(
        owner_id=OTHER_ACTOR,
        actor_role="employee",
        guide_assets=[{"key": KEY}],
        member_rows=[_member()],
    )
    objects = GuideObjects(data.events)
    reply = await ConfigurationManager(data, guide_objects=objects).execute(
        _request("guide_asset_sign", {"key": KEY})
    )

    assert isinstance(reply, ConfigurationGuideSuccess)
    assert reply.result.refreshed is False
    result = configuration_guide_data(reply)
    assert isinstance(result, ConfigurationGuideSignData)
    assert result.expires_in == 3600
    assert _action_names(data) == ["guide_shared", "guide_member"]
    assert any(event[0] == "get_url" for event in data.events)
    assert data.transactions[0].rollback_on_exit is False


@pytest.mark.asyncio
async def test_independent_sign_marks_rollback_only_after_signed_url_is_validated():
    data = GuideData(independent=True, guide_assets=[{"key": KEY}])
    objects = GuideObjects(data.events)
    reply = await ConfigurationManager(data, guide_objects=objects).execute(
        _request("guide_asset_sign", {"key": KEY})
    )

    assert isinstance(reply, ConfigurationGuideSuccess)
    assert reply.result.refreshed is False
    assert data.transactions[0].rollback_on_exit is True
    assert data.events[-1] == ("transaction_exit", 1, "rollback")


@pytest.mark.asyncio
async def test_nonmember_and_malformed_member_are_decided_before_the_storage_read():
    for rows, expected in (
        ([], 404),
        ([{**_member(), "assigned_at": "10000-01-01T00:00:00+00:00"}], None),
    ):
        data = GuideData(owner_id=OTHER_ACTOR, actor_role="employee", member_rows=rows)
        objects = GuideObjects(data.events)
        request = _request("guide_asset_sign", {"key": KEY})
        if expected is None:
            with pytest.raises(OverflowError):
                await ConfigurationManager(data, guide_objects=objects).execute(request)
        else:
            reply = await ConfigurationManager(data, guide_objects=objects).execute(
                request
            )
            assert isinstance(reply, ConfigurationFailure)
            assert reply.error.status_code == expected
        assert not any(event[0] == "get_url" for event in data.events)


@pytest.mark.asyncio
async def test_missing_port_is_checked_after_sign_authority_and_asset_registration():
    data = GuideData(guide_assets=[{"key": KEY}])
    with pytest.raises(ConfigurationUnavailable):
        await ConfigurationManager(data).execute(
            _request("guide_asset_sign", {"key": KEY})
        )
    assert _action_names(data) == ["guide_shared"]

    data = GuideData(guide_assets=[])
    reply = await ConfigurationManager(data).execute(
        _request("guide_asset_sign", {"key": KEY})
    )
    assert isinstance(reply, ConfigurationFailure)
    assert reply.error.status_code == 404
    assert _action_names(data) == ["guide_shared"]


@pytest.mark.asyncio
async def test_postcommit_refresh_failure_is_plain_500_marker_and_does_not_reauthorize():
    data = GuideData()

    async def missing_refresh(request_json, action, args, columns, values):
        facts = await GuideTransaction.step(
            tx, request_json, action, args, columns, values
        )
        if action == "guide_refresh":
            facts["facts"]["row_json"] = None
        return facts

    tx = GuideTransaction(data)
    tx.step = missing_refresh
    data.transaction = _single_transaction(tx, data)
    manager = ConfigurationManager(data)
    request = _request("guide_asset_delete", {"key": KEY})

    with pytest.raises(ConfigurationPostCommitFailure):
        await manager.refresh_guide(request)
    assert _action_names(data) == ["guide_refresh"]


def _single_transaction(transaction, data):
    @asynccontextmanager
    async def context(*, write):
        assert write is True
        data.events.append(("transaction_start", 1))
        try:
            yield transaction
        except BaseException:
            data.events.append(("transaction_exception", 1))
            raise
        else:
            data.events.append(("transaction_exit", 1, "borrowed_exit"))

    return context


def test_guide_request_and_reply_helpers_are_closed_raw_and_scope_bound():
    request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": {"raw": "preserved"}, "content_type": []},
    )
    assert GUIDE_OPERATIONS == {
        "guide_asset_upload_init",
        "guide_asset_upload_complete",
        "guide_asset_delete",
        "guide_asset_sign",
    }
    assert configuration_guide_payload(request) == {
        "key": KEY,
        "original_name": {"raw": "preserved"},
        "content_type": [],
    }
    assert request.target_id == PROJECT
    assert request.selectors.source_project_id is None
    assert request.selectors.template_id is None
    assert request.selectors.ml_backend_id is None
    assert request.selectors.ml_backend_present is False
    assert request.selectors.scope is None
    assert request.selectors.scope_present is False

    extra_payload = _request(
        "guide_asset_delete", {"key": KEY, "unrecognized": "value"}
    )
    with pytest.raises(ConfigurationUnavailable):
        configuration_guide_payload(extra_payload)

    valid_request = _request(
        "guide_asset_upload_complete",
        {"key": KEY, "original_name": "guide.png", "content_type": "image/png"},
    )
    reply = configuration_guide_success_reply(
        valid_request,
        resource_id=PROJECT,
        data={
            "key": KEY,
            "original_name": "guide.png",
            "content_type": "image/png",
            "size": 1,
            "uploaded_at": NOW,
        },
        refreshed=False,
    )
    altered = reply.model_dump(mode="json", by_alias=True)
    altered["result"]["resource_id"] = str(OTHER_ACTOR)
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(valid_request, altered)


def test_guide_reply_rejects_noncanonical_data_json_and_refresh_on_read_effects():
    init_request = _request(
        "guide_asset_upload_init",
        {"filename": "guide.png", "content_type": "image/png", "size": 1},
    )
    with pytest.raises(ConfigurationUnavailable):
        configuration_guide_success_reply(
            init_request,
            resource_id=PROJECT,
            data={
                "key": KEY,
                "upload_url": "https://signed.example.test/put",
                "expires_in": 900,
            },
            refreshed=True,
        )

    delete_request = _request("guide_asset_delete", {"key": KEY})
    reply = configuration_guide_success_reply(
        delete_request,
        resource_id=PROJECT,
        data={"deleted": KEY},
        refreshed=False,
    )
    tampered = reply.model_dump(mode="json", by_alias=True)
    tampered["result"]["data_json"] = '{ "deleted" : ' + json.dumps(KEY) + " }"
    with pytest.raises(ConfigurationUnavailable):
        decode_configuration_reply(delete_request, tampered)


def test_guide_schema_adds_four_operations_without_widening_legacy_success():
    schema = configuration_schema()
    stored = json.loads(
        files("aap_workspace")
        .joinpath("configuration_protocol.schema.json")
        .read_text(encoding="utf-8")
    )
    assert stored == schema
    operations = schema["$defs"]["ConfigurationRequest"]["properties"]["operation"][
        "enum"
    ]
    assert set(operations) >= GUIDE_OPERATIONS
    legacy = schema["$defs"]["ConfigurationSuccess"]["properties"]["operation"]["enum"]
    assert not set(legacy) & GUIDE_OPERATIONS
    guide_contract = schema["x-guide-asset-data-json"]
    assert guide_contract["canonical_ascii_json"] is True
    assert set(guide_contract["operations"]) == GUIDE_OPERATIONS
    assert (
        schema["$defs"]["ConfigurationGuideUploadCompleteData"]["additionalProperties"]
        is False
    )
