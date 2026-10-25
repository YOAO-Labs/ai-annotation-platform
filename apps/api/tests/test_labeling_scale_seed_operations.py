"""Fixed Labeling operations used by the local scale-seed script."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from aap_labeling import labeling_scale_seed_operations_schema
from app.services.labeling_scale_seed_operations import LabelingScaleSeedOperations


class _ScaleSeedSession:
    def __init__(self):
        self.calls = []

    async def execute(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))

    async def scalar(self, statement, parameters=None):
        self.calls.append((str(statement), parameters))
        return len(self.calls)


@pytest.mark.asyncio
async def test_adapter_uses_fixed_scale_seed_operations_and_public_batch_rows():
    db = _ScaleSeedSession()
    operations = LabelingScaleSeedOperations(db)
    project_id = uuid.uuid4()
    dataset_id = uuid.uuid4()
    annotator_id = uuid.uuid4()

    await operations.purge_labeling_rows("scale-seed", [project_id])
    await operations.create_batches(
        "scale-seed", project_id, dataset_id, 3, annotator_id
    )
    await operations.create_tasks("scale-seed", project_id, dataset_id)
    await operations.create_annotations("scale-seed", project_id, annotator_id, 2)
    await operations.read_first_batch_id(project_id)

    for function, (statement, _parameters) in zip(
        (
            "purge_scale_seed_labeling_rows",
            "create_scale_seed_batches",
            "create_scale_seed_tasks",
            "create_scale_seed_annotations",
        ),
        db.calls[:4],
        strict=True,
    ):
        assert f"aap_labeling_v1.{function}" in statement
    assert "SELECT id FROM aap_labeling_v1.batch_rows" in db.calls[-1][0]
    assert "FROM aap_labeling_v1.batch_rows" in db.calls[-1][0]
    assert all(
        not any(
            f"FROM public.{table}" in statement
            or f"INTO public.{table}" in statement
            or f"DELETE FROM public.{table}" in statement
            for table in ("tasks", "task_batches", "annotations")
        )
        for statement, _parameters in db.calls
    )


def test_scale_seed_contract_and_migration_resources_are_frozen():
    root = Path(__file__).resolve().parents[3]
    api_sql = root / "apps/api/alembic/sql/0364_labeling_scale_seed_operations.sql"
    package_sql = (
        root
        / "apps/labeling/src/aap_labeling/sql/0364_labeling_scale_seed_operations.sql"
    )
    api_schema = (
        root / "apps/api/alembic/sql/0364_labeling_scale_seed_operations.schema.json"
    )
    package_schema = (
        root / "apps/labeling/src/aap_labeling/scale-seed-operations.schema.json"
    )

    assert api_sql.read_bytes() == package_sql.read_bytes()
    schema = json.loads(api_schema.read_text(encoding="utf-8"))
    assert schema == json.loads(package_schema.read_text(encoding="utf-8"))
    assert schema == labeling_scale_seed_operations_schema()
    migration = (
        root / "apps/api/alembic/versions/0364_labeling_scale_seed_operations.py"
    ).read_text(encoding="utf-8")
    assert 'revision = "0364"' in migration
    assert 'down_revision = "0361"' in migration
    sql = api_sql.read_text(encoding="utf-8")
    assert "ORDER BY item.file_name" in sql
    assert "aap_assets_v1.scale_seed_dataset_item_facts AS item" in sql
    assert (
        "FROM public.dataset_items"
        not in sql.split("CREATE FUNCTION aap_labeling_v1.create_scale_seed_tasks", 1)[
            1
        ]
    )
    assert (
        sql.index("DELETE FROM public.annotations")
        < sql.index("DELETE FROM public.tasks")
        < sql.index("DELETE FROM public.task_batches")
    )
