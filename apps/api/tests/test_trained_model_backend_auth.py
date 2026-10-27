import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.services.ml_backend import MLBackendService
from app.services.trained_models import enabled_backend


@pytest.mark.parametrize(
    ("method", "token"),
    [("none", None), ("token", None), ("token", ""), ("token", "a" * 31)],
)
async def test_missing_controlled_auth_explains_where_to_configure(
    monkeypatch, method, token
):
    backend = SimpleNamespace(auth_method=method, auth_token=token)
    db = AsyncMock()
    db.get.return_value = backend
    monkeypatch.setattr(MLBackendService, "is_enabled", AsyncMock(return_value=True))

    with pytest.raises(HTTPException) as error:
        await enabled_backend(db, uuid.uuid4(), uuid.uuid4())

    assert error.value.status_code == 422
    assert "至少 32 个字符" in error.value.detail
    assert "YOLO_MANAGED_MODELS_TOKEN" in error.value.detail
    assert "系统托管" in error.value.detail


@pytest.mark.parametrize("method", ["token", "managed:yolo"])
async def test_configured_controlled_auth_keeps_backend_usable(monkeypatch, method):
    backend = SimpleNamespace(auth_method=method, auth_token="a" * 64)
    db = AsyncMock()
    db.get.return_value = backend
    monkeypatch.setattr(MLBackendService, "is_enabled", AsyncMock(return_value=True))

    assert await enabled_backend(db, uuid.uuid4(), uuid.uuid4()) is backend
