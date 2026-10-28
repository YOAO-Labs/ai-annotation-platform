from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.services.inference_backend_control import BackendControlService, BackendControlUnavailable


@pytest.mark.asyncio
async def test_terminal_receipt_must_match_exact_requested_boot_and_operation():
    registry_id, operation_id = uuid4(), uuid4()
    receipt = {
        "registry_id": str(registry_id), "boot_id": "boot-1",
        "operation_id": str(operation_id), "control_epoch": None, "generation": None,
        "pool_ids": ["models"], "unloaded": True,
    }
    service = object.__new__(BackendControlService)
    service._execute = AsyncMock(return_value=receipt)
    result = await service.terminal_drain(registry_id, boot_id="boot-1", operation_id=operation_id)
    assert result == receipt
    service._execute.assert_awaited_once_with(
        "terminal_drain", registry_id,
        body={"registry_id": str(registry_id), "boot_id": "boot-1", "operation_id": str(operation_id)},
    )
    service._execute.return_value = {**receipt, "boot_id": "replacement"}
    with pytest.raises(BackendControlUnavailable):
        await service.terminal_drain(registry_id, boot_id="boot-1", operation_id=operation_id)
    service._execute.return_value = {**receipt, "unloaded": False}
    with pytest.raises(BackendControlUnavailable):
        await service.terminal_drain(registry_id, boot_id="boot-1", operation_id=operation_id)
