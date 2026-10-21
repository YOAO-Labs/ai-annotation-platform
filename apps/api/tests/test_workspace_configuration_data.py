from contextlib import nullcontext
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from aap_workspace.configuration import ConfigurationCommandFailure
from aap_workspace.configuration_contract import configuration_request
from app.services.workspace_configuration_data import WorkspaceConfigurationData

pytestmark = pytest.mark.asyncio


class BusyDatabaseError(Exception):
    sqlstate = "55P03"
    hint = "aap.workspace.configuration.account_busy"


class Session:
    def __init__(self, autoflush):
        self.autoflush = autoflush
        self.no_autoflush = nullcontext()
        self.flush = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()
        self.close = AsyncMock()
        self.begin_nested = AsyncMock()
        self.scalar = AsyncMock(
            side_effect=[
                {"schema": "aap-workspace-configuration/v1"},
                {"opaque_native_reply": True},
            ]
        )


@pytest.mark.parametrize("autoflush", [True, False])
async def test_borrowed_transaction_keeps_commit_and_rollback_with_caller(autoflush):
    session = Session(autoflush)
    data = WorkspaceConfigurationData(session)
    request = configuration_request("create_project", uuid4(), None, {})
    async with data.transaction(write=True) as tx:
        await tx.step(
            request.model_dump(mode="json", by_alias=True), "begin", {}, [], []
        )
    assert session.flush.await_count == int(autoflush)
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize("autoflush", [True, False])
async def test_class_rename_prepare_never_flushes_or_owns_the_borrowed_transaction(
    autoflush,
):
    session = Session(autoflush)
    data = WorkspaceConfigurationData(session)
    assert data.independent is False
    result = await data.step(
        {"operation": "rename_project_class"},
        "class_rename_prepare",
        {},
        [],
        [],
    )
    assert result == {"schema": "aap-workspace-configuration/v1"}
    assert session.scalar.await_count == 1
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
    session.close.assert_not_awaited()
    session.begin_nested.assert_not_awaited()


async def test_project_insert_hook_flushes_even_when_autoflush_is_disabled():
    session = Session(False)
    await WorkspaceConfigurationData(session).before_project_insert()
    session.flush.assert_awaited_once()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


async def test_busy_native_account_lock_preserves_failed_caller_transaction():
    session = Session(False)
    session.scalar.side_effect = [
        {"schema": "aap-workspace-configuration/v1"},
        DBAPIError("native step", {}, BusyDatabaseError()),
    ]
    data = WorkspaceConfigurationData(session)
    request = configuration_request("create_project", uuid4(), None, {})
    with pytest.raises(ConfigurationCommandFailure) as caught:
        async with data.transaction(write=True) as tx:
            await tx.step(
                request.model_dump(mode="json", by_alias=True), "begin", {}, [], []
            )
    assert caught.value.error.detail == {"reason": "account_busy"}
    assert caught.value.error.rollback is False
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    "operation,mapped",
    [
        ("create_project", False),
        ("update_template", True),
    ],
)
async def test_preliminary_flush_busy_mapping_matches_account_read_kind(
    operation, mapped
):
    session = Session(True)
    failure = DBAPIError("pending flush", {}, BusyDatabaseError())
    session.flush.side_effect = failure
    data = WorkspaceConfigurationData(session)
    target = None if operation.startswith("create_") else uuid4()
    request = configuration_request(operation, uuid4(), target, {})
    expected = ConfigurationCommandFailure if mapped else DBAPIError
    with pytest.raises(expected):
        async with data.transaction(write=True) as tx:
            await tx.step(
                request.model_dump(mode="json", by_alias=True), "begin", {}, [], []
            )
    session.scalar.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    "action,expected", [("backend", "unavailable"), ("finish", "database")]
)
async def test_missing_probe_key_is_unavailable_only_for_backend_action(
    action, expected
):
    from aap_workspace.configuration_contract import ConfigurationUnavailable

    class MissingKeyError(Exception):
        sqlstate = "55000"

    session = Session(False)
    session.scalar.side_effect = DBAPIError("native step", {}, MissingKeyError())
    exception = ConfigurationUnavailable if expected == "unavailable" else DBAPIError
    with pytest.raises(exception):
        await WorkspaceConfigurationData(session).step({}, action, {}, [], [])
    session.flush.assert_not_awaited()
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()
