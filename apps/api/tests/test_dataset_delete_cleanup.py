from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.dataset import DatasetService
from app.services.storage import storage_service


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return self

    def all(self) -> list[object]:
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FailingFlushSession:
    def __init__(self, dataset: object, results: list[_ScalarResult]) -> None:
        self.dataset = dataset
        self.results = iter(results)
        self.deleted: list[object] = []

    async def get(self, _model, _dataset_id):
        return self.dataset

    async def execute(self, _query):
        return next(self.results)

    async def scalar(self, _query):
        return None

    async def delete(self, value: object) -> None:
        self.deleted.append(value)

    async def flush(self) -> None:
        raise RuntimeError("foreign key rejection")


async def test_dataset_delete_defers_storage_cleanup_until_database_commit(
    monkeypatch,
):
    dataset = SimpleNamespace(id=uuid.uuid4())
    db = _FailingFlushSession(
        dataset,
        [_ScalarResult([uuid.uuid4()])],
    )
    delete_object = MagicMock()
    delete_prefix = MagicMock()
    monkeypatch.setattr(storage_service, "delete_object", delete_object)
    monkeypatch.setattr(storage_service, "delete_prefix", delete_prefix)

    with pytest.raises(RuntimeError, match="foreign key rejection"):
        await DatasetService(db).delete(dataset.id)  # type: ignore[arg-type]

    assert db.deleted == [dataset]
    delete_object.assert_not_called()
    delete_prefix.assert_not_called()
