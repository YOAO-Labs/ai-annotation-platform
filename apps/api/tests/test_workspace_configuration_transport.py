"""Unexpected in-process owner failures are not mapped to unavailability."""

from __future__ import annotations

import uuid

import pytest

from app.services import workspace_configuration as commands

pytestmark = pytest.mark.asyncio


async def test_borrowed_configuration_keeps_raw_business_value_error(monkeypatch):
    class Manager:
        async def execute(self, request):
            raise ValueError("Backend pool missing")

    monkeypatch.setattr(commands, "_configuration_manager", lambda db: Manager())
    with pytest.raises(ValueError, match="Backend pool missing"):
        await commands.execute_configuration(
            object(), operation="create_project", actor_id=uuid.uuid4(), payload={}
        )
