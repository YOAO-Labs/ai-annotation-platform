"""The health observation owner is selected only through HTTP credentials."""

from __future__ import annotations

import os
import uuid

from fastapi import HTTPException
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.ml_backend_registry import MLBackendRegistry
from app.services.ml_backend import MLBackendService
from tests.inference_health_observation_helpers import clear_observation_selection
from tests.labeling_class_query_helpers import assert_owned_test_database

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires verified disposable module-data resources",
    ),
]


def _configure(monkeypatch, **values) -> None:
    from app.config import settings

    clear_observation_selection(monkeypatch)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


async def _assert_unavailable(test_engine, monkeypatch) -> None:
    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    async with maker() as caller:
        row = MLBackendRegistry(
            id=uuid.uuid4(),
            name="Selector health",
            url=f"http://selector-{uuid.uuid4().hex}.example.test",
            auth_method="token",
            auth_token="selector-token",
            extra_params={},
        )
        caller.add(row)
        await caller.flush()
        with pytest.raises(HTTPException) as caught:
            await MLBackendService(caller).check_health(row.id)
        assert caught.value.status_code == 503
        assert caught.value.detail == {
            "error_code": "inference_health_observation_unavailable"
        }
        await caller.rollback()


async def test_legacy_api_database_selectors_are_removed():
    from app.config import settings

    # The only selector is the HTTP owner; a DSN binding must not come back.
    assert not hasattr(settings, "inference_health_observation_database_url")
    assert not hasattr(settings, "inference_health_observation_database_url_file")


async def test_http_owner_url_is_required(test_engine, monkeypatch):
    await assert_owned_test_database(test_engine)
    _configure(monkeypatch)
    await _assert_unavailable(test_engine, monkeypatch)


@pytest.mark.parametrize("token_file", ["missing", "empty"])
async def test_token_file_must_be_readable_and_nonempty(
    test_engine, monkeypatch, tmp_path, token_file
):
    await assert_owned_test_database(test_engine)
    path = tmp_path / "token"
    if token_file == "empty":
        path.write_text("", encoding="utf-8")
    _configure(
        monkeypatch,
        inference_health_observation_url="http://127.0.0.1:9",
        inference_health_observation_token_file=str(path),
    )
    await _assert_unavailable(test_engine, monkeypatch)


async def test_direct_and_file_tokens_are_mutually_exclusive(
    test_engine, monkeypatch, tmp_path
):
    await assert_owned_test_database(test_engine)
    path = tmp_path / "token"
    path.write_text("file-token", encoding="utf-8")
    _configure(
        monkeypatch,
        inference_health_observation_url="http://127.0.0.1:9",
        inference_health_observation_token="direct-token",
        inference_health_observation_token_file=str(path),
    )
    await _assert_unavailable(test_engine, monkeypatch)


@pytest.mark.parametrize("token", ["", "owner-token"])
async def test_url_without_usable_http_owner_is_unavailable(
    test_engine, monkeypatch, token
):
    await assert_owned_test_database(test_engine)
    _configure(
        monkeypatch,
        inference_health_observation_url="http://127.0.0.1:9",
        inference_health_observation_token=token,
    )
    await _assert_unavailable(test_engine, monkeypatch)
