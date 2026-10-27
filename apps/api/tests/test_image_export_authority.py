"""Capture reference delegation from measured facts without rehydrating scope."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.services.content_snapshot import ContentError
from app.services.exporting import reference_media
from app.services.reference_keys import reference_key


def fixture():
    project = SimpleNamespace(id=uuid.uuid4(), data_type="image")
    actor = SimpleNamespace(id=uuid.uuid4())
    request = SimpleNamespace(state=SimpleNamespace(api_key_id=None))
    task, item = uuid.uuid4(), uuid.uuid4()
    snapshot = {
        "schema": 3,
        "project_id": str(project.id),
        "batch_id": None,
        "task_ids": None,
        "tasks": [
            {
                "id": str(task),
                "batch_id": None,
                "dataset_item_id": str(item),
                "file_path": reference_key(item, "a" * 64),
                "file_name": "same.png",
                "version": 7,
            }
        ],
        "references": {
            str(task): {"asset_id": str(uuid.uuid4()), "sha256": "a" * 64, "size": 10}
        },
    }
    return project, actor, request, snapshot


class NoHydration:
    def __getattr__(self, name):
        raise AssertionError(
            "Frozen authority must not requery business/source rows: " + name
        )


async def test_frozen_reference_authority_keeps_original_binding_without_orm_reads(
    monkeypatch,
):
    project, actor, request, snapshot = fixture()
    authority = AsyncMock(return_value=None)
    monkeypatch.setattr(reference_media, "key_authority", authority)
    original = deepcopy(snapshot)
    result = await reference_media._capture_export_authority(
        NoHydration(), project, actor, request, frozen_snapshot=snapshot
    )
    authority.assert_awaited_once()
    assert result["tasks"][0]["version"] == 7
    assert result["tasks"][0]["reference"] == next(
        iter(snapshot["references"].values())
    )
    result["tasks"][0]["reference"]["size"] = 11
    assert snapshot == original


async def test_managed_frozen_scope_does_not_add_reference_key_scope(monkeypatch):
    project, actor, request, snapshot = fixture()
    snapshot["tasks"][0]["file_path"] = "controlled-original"
    snapshot["references"] = {}
    authority = AsyncMock()
    monkeypatch.setattr(reference_media, "key_authority", authority)
    assert (
        await reference_media._capture_export_authority(
            NoHydration(), project, actor, request, frozen_snapshot=snapshot
        )
        is None
    )
    authority.assert_not_awaited()


@pytest.mark.parametrize(
    "mutation", ["project", "batch", "selection", "missing_reference", "schema"]
)
async def test_frozen_delegation_refuses_a_different_scope_before_any_db_reads(
    mutation,
):
    project, actor, request, snapshot = fixture()
    if mutation == "project":
        snapshot["project_id"] = str(uuid.uuid4())
    elif mutation == "batch":
        snapshot["batch_id"] = str(uuid.uuid4())
    elif mutation == "selection":
        snapshot["task_ids"] = [snapshot["tasks"][0]["id"]]
    elif mutation == "missing_reference":
        snapshot["references"] = {}
    else:
        snapshot["schema"] = True
    with pytest.raises(ContentError, match="scope_changed"):
        await reference_media._capture_export_authority(
            NoHydration(), project, actor, request, frozen_snapshot=snapshot
        )
