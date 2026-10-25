"""Saved training targets protect locations and claim only their actual LIST bytes."""

from datetime import datetime, timedelta, timezone
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models.training_run import TrainingRun
from app.db.models.training_preview import TrainingPreview
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services import training_dataset_storage as owners
from app.services import storage_usage_categories as categories
from app.services.managed_s3_target import Target
from app.services.storage_location import location_is_referenced
from tests.test_training_dataset_storage import database_version


FAMILIES = {
    "training_dataset_storage": "training-dataset",
    "training_run_storage": "training-run",
    "training_preview_storage": "training-preview",
    "sample_evaluation_storage": "sample-evaluation",
    "active_learning_storage": "active-learning",
}


def target(location_id=None):
    return Target(
        location_id or uuid.uuid4(),
        1,
        1,
        categories.endpoint(),
        "listed-training",
        "公司/训练/_aap_managed/training/",
        purpose="training",
    )


def saved_case(service_name):
    identifier, project = uuid.uuid4(), uuid.uuid4()
    service = import_module("app.services." + service_name)
    ref = owners.make_ref(
        target(),
        2,
        service.prefix_for(project, identifier),
        purpose=FAMILIES[service_name],
    )
    data = {"id": str(identifier), "project_id": str(project), "storage_ref": ref}
    index = next(
        i
        for i, family in enumerate(categories.FAMILIES)
        if family.service == service_name
    )
    return index, data, ref["prefix"] + "retained.bin"


@pytest.mark.parametrize("service_name", FAMILIES)
async def test_rooted_training_bytes_require_exact_owner_root_endpoint_and_identity(
    service_name, monkeypatch
):
    index, data, key = saved_case(service_name)
    family = categories.FAMILIES[index]
    db = SimpleNamespace(execute=AsyncMock(return_value=[(f"ref:{index}", data)]))
    wrong_root = key.replace("公司/训练/", "其他/训练/", 1)
    objects = [{"key": key, "size": 7}, {"key": wrong_root, "size": 11}]
    result = await categories.classify(db, "listed-training", objects)
    assert result["categories"][family.category] == {"object_count": 1, "size_bytes": 7}
    assert result["categories"]["unclassified"] == {"object_count": 1, "size_bytes": 11}
    monkeypatch.setattr(categories, "endpoint", lambda: "https://other.example.test")
    result = await categories.classify(db, "listed-training", objects)
    assert result["categories"]["unclassified"] == {"object_count": 2, "size_bytes": 18}


@pytest.mark.parametrize("service_name", FAMILIES)
async def test_a_different_record_cannot_claim_the_same_saved_training_prefix(
    service_name,
):
    index, data, key = saved_case(service_name)
    data["project_id"] = str(uuid.uuid4())
    db = SimpleNamespace(execute=AsyncMock(return_value=[(f"ref:{index}", data)]))
    result = await categories.classify(db, "listed-training", [{"key": key, "size": 7}])
    assert result["categories"]["unclassified"] == {"object_count": 1, "size_bytes": 7}


@pytest.mark.parametrize(
    "service_name", ["training_dataset_storage", "training_run_storage"]
)
async def test_training_deletion_keeps_its_saved_endpoint_after_business_row_is_gone(
    service_name, monkeypatch
):
    _, data, key = saved_case(service_name)
    data = {"storage_ref": data["storage_ref"], "prefix": data["storage_ref"]["prefix"]}
    db = SimpleNamespace(execute=AsyncMock(return_value=[("training_deletion", data)]))
    result = await categories.classify(db, "listed-training", [{"key": key, "size": 7}])
    expected = (
        "training_version"
        if service_name == "training_dataset_storage"
        else "other_platform"
    )
    assert result["categories"][expected] == {"object_count": 1, "size_bytes": 7}
    monkeypatch.setattr(categories, "endpoint", lambda: "https://other.example.test")
    result = await categories.classify(db, "listed-training", [{"key": key, "size": 7}])
    assert result["categories"]["unclassified"] == {"object_count": 1, "size_bytes": 7}


@pytest.mark.parametrize(
    "table",
    [
        "training_dataset_versions",
        "training_runs",
        "training_previews",
        "sample_evaluation_runs",
        "active_learning_rounds",
        "training_storage_deletions",
    ],
)
async def test_location_protection_does_not_drop_a_saved_training_family_by_business_status(
    table,
):
    location = uuid.uuid4()

    async def scalar(query):
        entity = query.column_descriptions[0]["entity"]
        if entity.__tablename__ != table:
            return None
        sql = str(
            query.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert str(location) in sql and "storage_ref" in sql
        assert "status" not in sql
        return uuid.uuid4()

    assert await location_is_referenced(
        SimpleNamespace(scalar=AsyncMock(side_effect=scalar)), location
    )


@pytest.mark.parametrize("kind", ["version", "run", "preview", "deletion"])
async def test_real_training_rows_protect_and_classify_until_their_saved_owner_is_removed(
    db_session, super_admin, kind
):
    version = await database_version(db_session, super_admin[0])
    current = target()
    if kind == "version":
        row, purpose, namespace, category = (
            version,
            "training-dataset",
            "datasets",
            "training_version",
        )
    elif kind == "run":
        row = TrainingRun(
            id=uuid.uuid4(),
            project_id=version.project_id,
            dataset_id=version.id,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            name="Saved target",
            config={},
            config_digest="b" * 64,
            status="cancelled",
        )
        purpose, namespace, category = "training-run", "runs", "other_platform"
    elif kind == "preview":
        row = TrainingPreview(
            id=uuid.uuid4(),
            project_id=version.project_id,
            dataset_id=version.id,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
            request={},
            input_digest="b" * 64,
            spec={},
            authorization={},
            resource_id="test/preview",
            status="expired",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        purpose, namespace, category = "training-preview", "previews", "preview_cache"
    else:
        row = TrainingStorageDeletion(id=uuid.uuid4(), bucket=current.bucket, prefix="")
        purpose, namespace, category = (
            "training-dataset",
            "datasets",
            "training_version",
        )
    logical = f"projects/{version.project_id}/{namespace}/{row.id}/"
    row.storage_ref = owners.make_ref(current, 2, logical, purpose=purpose)
    if kind == "deletion":
        row.prefix = row.storage_ref["prefix"]
    db_session.add(row)
    await db_session.flush()
    assert await location_is_referenced(db_session, current.location_id)
    assert not await location_is_referenced(db_session, uuid.uuid4())
    key = row.storage_ref["prefix"] + "retained.bin"
    result = await categories.classify(
        db_session, current.bucket, [{"key": key, "size": 7}]
    )
    assert result["categories"][category] == {"object_count": 1, "size_bytes": 7}
    await db_session.delete(row)
    await db_session.flush()
    assert not await location_is_referenced(db_session, current.location_id)
