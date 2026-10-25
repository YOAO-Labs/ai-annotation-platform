"""Preview output ownership, safe old-lease settlement and retained cleanup."""

from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import HTTPException
from PIL import Image
import pytest
from training_contract.preview import POLICY, POLICY_DIGEST

from app.api.v1 import training_previews as api
from app.config import settings
from app.schemas.training_preview import (
    PreviewArtifact,
    PreviewClaim,
    PreviewFinish,
    TrainingPreviewCreate,
)
from app.services import training_dataset_storage, training_preview_storage
from app.services import training_previews as previews
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.storage import storage_service
from app.services.training_dataset_bridge import content_digest


@pytest.fixture(autouse=True)
def legacy_transport_boundary(monkeypatch):
    # These pre-existing unit cases isolate business ownership from admission.
    from app.services import training_storage_io
    from app.services.storage import storage_service

    async def run(db, owner, purpose, callback, *args, **kwargs):
        return callback(storage_service, *args, **kwargs)

    monkeypatch.setattr(training_storage_io, "run", run)


class Objects:
    def __init__(self, endpoint):
        self.meta = SimpleNamespace(endpoint_url=endpoint)
        self.objects, self.calls = {}, []

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Bucket, Key))
        raw, metadata = self.objects[Bucket, Key]
        return {
            "ContentLength": len(raw),
            "Body": io.BytesIO(raw),
            "Metadata": metadata,
        }

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.calls.append((operation, Params["Bucket"], Params["Key"]))
        return f"https://owned-storage/{Params['Bucket']}/{Params['Key']}"


def reference(
    project_id, identifier, *, purpose="training-preview", bucket="old-preview"
):
    endpoint = (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )
    prefix_for = (
        training_preview_storage.prefix_for
        if purpose == "training-preview"
        else training_dataset_storage.prefix_for
    )
    return {
        "schema": 1,
        "purpose": purpose,
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": bucket,
        "prefix": prefix_for(project_id, identifier),
        "storage_identity": managed_storage_identity(endpoint, bucket),
        "origin": "new",
        "evidence": None,
    }


@pytest.fixture
def owned(monkeypatch):
    identifier, project_id, dataset_id, source_id = [uuid.uuid4() for _ in range(4)]
    request = TrainingPreviewCreate(
        dataset_id=dataset_id,
        framework="ultralytics",
        base_model="yolo11n",
        configuration={"schema_id": "preview-test", "schema_digest": "a" * 64},
        request_id=uuid.uuid4(),
    ).model_dump(mode="json")
    config = {
        "configuration": {
            **request["configuration"],
            "effective_values": {"epochs": 10, "close_mosaic": 0},
        },
        "classes": ["car"],
        "framework_version": "8.4.177",
        "image_digest": "sha256:" + "e" * 64,
        "adapter_revision": 2,
        "dataset_sha256": "d" * 64,
    }
    source = {
        "task_id": str(source_id),
        "media_key": f"projects/{project_id}/datasets/{dataset_id}/media/{source_id}",
        "image_path": f"images/train/{source_id}.png",
        "label_path": f"labels/train/{source_id}.txt",
        "labels": "0 0.5 0.5 0.5 0.5\n",
        "sha256": "b" * 64,
        "label_sha256": "c" * 64,
        "size_bytes": 100,
        "width": 16,
        "height": 12,
    }
    frozen = {
        "config": config,
        "config_digest": content_digest(config),
        "native_values": {
            name: 0.0 for name in ("mosaic", "mixup", "cutmix", "copy_paste")
        },
        "dataset_snapshot_digest": "f" * 64,
        "seed": request["seed"],
        "epoch": request["epoch"],
        "count": 1,
        "sources": [source],
        "pool_summary": {"selected": 1},
    }
    preview = SimpleNamespace(
        id=identifier,
        project_id=project_id,
        dataset_id=dataset_id,
        creator_id=uuid.uuid4(),
        async_job_id=uuid.uuid4(),
        request=request,
        request_id=uuid.UUID(request["request_id"]),
        request_digest=content_digest(request),
        input_digest="1" * 64,
        spec=frozen,
        authorization={"api_key_id": None},
        status="running",
        resource_id=previews.resource_id(),
        executor_id="preview-flow",
        boot_id=uuid.uuid4(),
        claim_id=uuid.uuid4(),
        lease_digest=None,
        finish_digest=None,
        runtime={},
        upload_inventory={},
        artifacts={},
        error_message=None,
        heartbeat_at=previews.now(),
        started_at=previews.now(),
        completed_at=None,
        put_expires_at=None,
        expires_at=previews.now() + timedelta(hours=1),
        created_at=previews.now(),
        storage_ref=reference(project_id, identifier),
    )
    dataset = SimpleNamespace(
        id=dataset_id,
        project_id=project_id,
        snapshot_digest=frozen["dataset_snapshot_digest"],
        package_sha256=config["dataset_sha256"],
        storage_ref=reference(
            project_id, dataset_id, purpose="training-dataset", bucket="frozen-inputs"
        ),
    )
    client = Objects(preview.storage_ref["endpoint_url"])
    monkeypatch.setattr(storage_service, "client", client)
    monkeypatch.setattr(storage_service, "training_bucket", "new-current-bucket")
    return SimpleNamespace(preview=preview, dataset=dataset, client=client)


def evidence(owned):
    row = owned.preview
    with io.BytesIO() as buffer:
        Image.new("RGB", (16, 12), "orange").save(buffer, format="PNG")
        png = buffer.getvalue()
    frozen = row.spec
    source = frozen["sources"][0]["task_id"]
    report = {
        "revision": 1,
        "preview_id": str(row.id),
        "input_digest": row.input_digest,
        "config_digest": frozen["config_digest"],
        "policy": POLICY,
        "policy_digest": POLICY_DIGEST,
        "seed": frozen["seed"],
        "epoch": frozen["epoch"],
        "mosaic_closed": False,
        "native_parameters": frozen["native_values"],
        "transform_parameters": frozen["native_values"],
        "source_pool": frozen["sources"],
        "pool_summary": frozen["pool_summary"],
        "runtime": {
            "framework_version": "8.4.177",
            "image_digest": frozen["config"]["image_digest"],
            "adapter_revision": 2,
            "pipeline_sha256": "f" * 64,
            "cuda_initialized": False,
        },
        "outputs": [
            {
                "index": 0,
                "image": "preview-0.png",
                "sha256": hashlib.sha256(png).hexdigest(),
                "width": 16,
                "height": 12,
                "boxes": [{"class_index": 0, "xywh": [0.5, 0.5, 0.5, 0.5]}],
                "source_draws": [source],
                "sources_read": [source],
                "operations": [],
                "filtered": 0,
                "clipped": 0,
            }
        ],
    }
    for name, raw in {
        "report.json": json.dumps(report).encode(),
        "preview-0.png": png,
    }.items():
        spec = {
            "name": name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
        }
        key = row.storage_ref["prefix"] + name
        row.upload_inventory[name] = spec
        row.artifacts[name] = {**spec, "key": key}
        owned.client.objects[row.storage_ref["bucket"], key] = (
            raw,
            {
                "preview-id": str(row.id),
                "input-digest": row.input_digest,
                "sha256": spec["sha256"],
            },
        )
        owned.client.objects["new-current-bucket", key] = (b"same-key trap", {})
    return report


def finish_body(row, status="succeeded"):
    return PreviewFinish(
        executor_id=row.executor_id,
        boot_id=row.boot_id,
        lease_token="preview-flow-exact-fenced-32-character-token",
        status=status,
        process_exited=True,
    )


async def test_creation_allocates_once_after_id_before_job_and_replay_never_adopts(
    owned, monkeypatch
):
    row = owned.preview
    events, added = [], []

    async def flush():
        added[0].id = uuid.uuid4()
        events.append("flush")

    async def allocate(db, record):
        assert record.id is not None
        record.storage_ref = reference(record.project_id, record.id)
        events.append("allocate")

    async def job(*args, **kwargs):
        assert added[0].storage_ref is not None
        events.append("job")
        return SimpleNamespace(id=uuid.uuid4())

    db = SimpleNamespace(
        scalar=AsyncMock(side_effect=[None, 0]),
        get=AsyncMock(side_effect=[owned.dataset, SimpleNamespace(runtime={})]),
        add=added.append,
        flush=flush,
    )
    allocation = AsyncMock(side_effect=allocate)
    monkeypatch.setattr(training_preview_storage, "allocate", allocation)
    monkeypatch.setattr(
        previews.training, "frozen_config", AsyncMock(return_value=row.spec["config"])
    )
    monkeypatch.setattr(previews, "validate_request", Mock())
    monkeypatch.setattr(previews, "native_values", lambda _: row.spec["native_values"])
    monkeypatch.setattr(
        previews,
        "select_sources",
        lambda *_: (row.spec["sources"], row.spec["pool_summary"]),
    )
    monkeypatch.setattr(
        previews, "capabilities", AsyncMock(return_value={"online": True})
    )
    monkeypatch.setattr(previews.training, "runtime_compatible", lambda *_: True)
    monkeypatch.setattr(previews.jobs, "create_job", job)
    project, user = (
        SimpleNamespace(id=row.project_id),
        SimpleNamespace(id=row.creator_id),
    )
    body = TrainingPreviewCreate.model_validate(row.request)
    result = await previews.create(db, project, user, body, None)
    assert events == ["flush", "allocate", "job"]
    assert result is added[0]
    row.storage_ref = None
    db.scalar = AsyncMock(return_value=row)
    assert await previews.create(db, project, user, body, None) is row
    allocation.assert_awaited_once()
    assert events == ["flush", "allocate", "job"]


async def test_input_urls_keep_dataset_owner_and_require_preview_owner_first(owned):
    row = owned.preview
    db = SimpleNamespace(get=AsyncMock(return_value=owned.dataset), commit=AsyncMock())
    result = await previews.assignment(db, row)
    source = row.spec["sources"][0]
    assert result["source_urls"][source["task_id"]].startswith(
        "https://owned-storage/frozen-inputs/"
    )
    assert owned.client.calls == [("get_object", "frozen-inputs", source["media_key"])]
    row.storage_ref = None
    owned.client.calls.clear()
    db.get.reset_mock()
    with pytest.raises(HTTPException) as error:
        await previews.assignment(db, row)
    assert error.value.status_code == 409
    assert owned.client.calls == []
    db.get.assert_not_awaited()
    assert (await previews.assignment(db, row, stop=True))["source_urls"] == {}


async def test_upload_and_success_finish_use_saved_bucket(owned, monkeypatch):
    row = owned.preview
    raw = evidence(owned)
    inventory = list(row.upload_inventory.values())
    row.upload_inventory, row.artifacts = {}, {}
    lease = SimpleNamespace(preview_id=row.id)
    monkeypatch.setattr(previews, "fenced", AsyncMock(return_value=(lease, row)))
    monkeypatch.setattr(previews.training, "authority", AsyncMock())
    monkeypatch.setattr(previews, "terminal", AsyncMock())
    body = SimpleNamespace(artifacts=[PreviewArtifact(**spec) for spec in inventory])
    urls = await previews.uploads(SimpleNamespace(commit=AsyncMock()), row.id, body)
    assert all(
        target["url"].startswith("https://owned-storage/old-preview/")
        for target in urls.values()
    )
    await previews.finish(SimpleNamespace(commit=AsyncMock()), row.id, finish_body(row))
    assert row.artifacts["preview-0.png"]["sha256"] == raw["outputs"][0]["sha256"]
    assert lease.preview_id is None
    assert all(bucket == "old-preview" for _, bucket, _ in owned.client.calls)


@pytest.mark.parametrize(
    "invalid", ["missing-pin", "changed-endpoint", "foreign-prefix"]
)
async def test_unknown_output_owner_rejects_uploads_and_success_without_io(
    owned, monkeypatch, invalid
):
    row = owned.preview
    evidence(owned)
    inventory = list(row.upload_inventory.values())
    row.upload_inventory, row.artifacts = {}, {}
    if invalid == "missing-pin":
        row.storage_ref = None
    elif invalid == "changed-endpoint":
        owned.client.meta.endpoint_url = "https://unrelated-storage.invalid"
    else:
        row.storage_ref["prefix"] = reference(row.project_id, uuid.uuid4())["prefix"]
    lease = SimpleNamespace(preview_id=row.id)
    monkeypatch.setattr(previews, "fenced", AsyncMock(return_value=(lease, row)))
    monkeypatch.setattr(previews.training, "authority", AsyncMock())
    for action in [
        previews.uploads(
            None,
            row.id,
            SimpleNamespace(
                artifacts=[PreviewArtifact(**value) for value in inventory]
            ),
        ),
        previews.finish(SimpleNamespace(commit=AsyncMock()), row.id, finish_body(row)),
    ]:
        with pytest.raises(HTTPException) as error:
            await action
        assert error.value.status_code == 409
    assert row.upload_inventory == {} and row.artifacts == {}
    assert row.put_expires_at is None and lease.preview_id == row.id
    assert owned.client.calls == []


def test_report_and_image_reads_ignore_current_bucket_and_keep_physical_verification(
    owned,
):
    evidence(owned)
    report = previews.read_report(owned.preview)
    with previews.read_image(owned.preview, report.outputs[0]) as image:
        assert image.read().startswith(b"\x89PNG")
    assert all(bucket == "old-preview" for _, bucket, _ in owned.client.calls)
    key = owned.preview.artifacts["report.json"]["key"]
    raw, metadata = owned.client.objects["old-preview", key]
    owned.client.objects["old-preview", key] = (b"x" * len(raw), metadata)
    with pytest.raises(ValueError):
        previews.read_report(owned.preview)


@pytest.mark.parametrize("name", ["report.json", "preview-0.png"])
@pytest.mark.parametrize("invalid", ["foreign-prefix", "null-key"])
def test_invalid_artifact_key_is_rejected_before_get(owned, name, invalid):
    evidence(owned)
    report = previews.read_report(owned.preview)
    owned.client.calls.clear()
    owned.preview.artifacts[name]["key"] = (
        reference(owned.preview.project_id, uuid.uuid4())["prefix"] + name
        if invalid == "foreign-prefix"
        else None
    )
    with pytest.raises(HTTPException) as error:
        if name == "report.json":
            previews.read_report(owned.preview)
        else:
            previews.read_image(owned.preview, report.outputs[0])
    assert error.value.status_code == 409
    assert owned.client.calls == []


@pytest.mark.parametrize("name", ["report.json", "preview-0.png"])
def test_object_metadata_must_match_the_independent_sha_receipt(owned, name):
    evidence(owned)
    report = previews.read_report(owned.preview)
    key = owned.preview.artifacts[name]["key"]
    raw, metadata = owned.client.objects["old-preview", key]
    owned.client.objects["old-preview", key] = (
        raw,
        {**metadata, "sha256": "0" * 64},
    )
    with pytest.raises(ValueError):
        if name == "report.json":
            previews.read_report(owned.preview)
        else:
            previews.read_image(owned.preview, report.outputs[0])


@pytest.mark.parametrize("field", ["input", "source", "class", "operations", "runtime"])
def test_report_semantics_remain_pure_and_reject_mismatched_evidence(owned, field):
    raw = deepcopy(evidence(owned))
    assert (
        previews.validate_report(owned.preview, owned.preview.artifacts, raw)
        .outputs[0]
        .index
        == 0
    )
    if field == "input":
        raw["input_digest"] = "9" * 64
    elif field == "source":
        raw["outputs"][0]["sources_read"] = [str(uuid.uuid4())]
    elif field == "class":
        raw["outputs"][0]["boxes"][0]["class_index"] = 1
    elif field == "operations":
        raw["outputs"][0]["clipped"] = 1
    else:
        raw["runtime"]["cuda_initialized"] = True
    with pytest.raises(ValueError):
        previews.validate_report(owned.preview, owned.preview.artifacts, raw)
    assert owned.client.calls == []


async def test_unknown_owner_preserves_metadata_but_refuses_image_route(
    owned, monkeypatch
):
    row = owned.preview
    evidence(owned)
    row.status, row.storage_ref = "succeeded", None
    assert (await previews.output(row, db=None)).report is None
    monkeypatch.setattr(api, "lock_owner", AsyncMock())
    monkeypatch.setattr(api, "load", AsyncMock(return_value=row))
    db = SimpleNamespace(commit=AsyncMock())
    with pytest.raises(HTTPException) as error:
        await api.image(
            row.id, 0, project=SimpleNamespace(id=row.project_id), user=None, db=db
        )
    assert error.value.status_code == 409
    db.commit.assert_not_awaited()
    assert owned.client.calls == []


async def test_null_owner_claim_recovery_stops_without_input_signing(
    owned, monkeypatch
):
    row = owned.preview
    row.storage_ref = None
    claim = PreviewClaim(
        purpose="augmentation-preview",
        resource_id=previews.resource_id(),
        executor_id=row.executor_id,
        boot_id=row.boot_id,
        claim_id=row.claim_id,
        runtime={},
        recover_only=True,
    )
    row.lease_digest = hashlib.sha256(
        previews.token_for(row, row.claim_id).encode()
    ).hexdigest()
    lease = SimpleNamespace(preview_id=row.id)
    monkeypatch.setattr(previews, "lock_resource", AsyncMock(return_value=lease))
    monkeypatch.setattr(previews.training, "authority", AsyncMock())
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    result = await previews.claim(db, claim)
    assert result["stop"] is True and result["source_urls"] == {}
    assert result["lease_token"] == previews.token_for(row, row.claim_id)
    assert lease.preview_id == row.id and db.get.await_count == 1
    assert owned.client.calls == []


async def test_null_owner_queued_claim_fails_without_acquiring_cpu_or_inputs(
    owned, monkeypatch
):
    row = owned.preview
    row.status, row.storage_ref = "queued", None
    lease = SimpleNamespace(preview_id=None)
    monkeypatch.setattr(previews, "lock_resource", AsyncMock(return_value=lease))
    monkeypatch.setattr(previews, "qualified_runtime", lambda _: True)
    monkeypatch.setattr(previews.training, "runtime_compatible", lambda *_: True)
    terminal = AsyncMock()
    monkeypatch.setattr(previews, "terminal", terminal)
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: [row])),
        get=AsyncMock(),
    )
    claim = PreviewClaim(
        purpose="augmentation-preview",
        resource_id=previews.resource_id(),
        executor_id=row.executor_id,
        boot_id=row.boot_id,
        claim_id=row.claim_id,
        runtime={},
    )
    assert await previews.claim(db, claim) is None
    assert terminal.await_args.args[2] == "failed"
    db.get.assert_not_awaited()
    assert lease.preview_id is None and owned.client.calls == []


@pytest.mark.parametrize("status", ["failed", "cancelled", "expired-success"])
async def test_null_owner_heartbeat_and_terminal_receipt_can_settle(
    owned, monkeypatch, status
):
    row = owned.preview
    row.storage_ref = None
    lease = SimpleNamespace(preview_id=row.id, last_seen_at=None)
    monkeypatch.setattr(previews, "fenced", AsyncMock(return_value=(lease, row)))
    monkeypatch.setattr(previews.training, "authority", AsyncMock())
    request_cancel = AsyncMock()
    monkeypatch.setattr(previews.jobs, "request_cancel", request_cancel)
    heartbeat = await previews.heartbeat(None, row.id, None)
    assert heartbeat["stop"] and row.status == "cancelling"
    request_cancel.assert_awaited_once()
    if status == "expired-success":
        row.expires_at = previews.now() - timedelta(seconds=1)
        body = finish_body(row)
    else:
        row.status = "running"
        body = finish_body(row, status)

    async def terminal(db, row, value, error):
        row.status = value

    monkeypatch.setattr(previews, "terminal", terminal)
    await previews.finish(SimpleNamespace(commit=AsyncMock()), row.id, body)
    assert lease.preview_id is None
    assert row.status == ("cancelled" if status == "expired-success" else status)
    assert (
        await previews.finish(SimpleNamespace(commit=AsyncMock()), row.id, body) is row
    )
    with pytest.raises(HTTPException) as error:
        await previews.finish(
            None, row.id, body.model_copy(update={"error_message": "changed receipt"})
        )
    assert error.value.status_code == 409 and owned.client.calls == []


async def test_cleanup_preserves_unknown_pending_and_leased_rows_and_continues_after_io_failure(
    owned, monkeypatch
):
    def expired(**updates):
        row = deepcopy(owned.preview)
        row.id, row.status = uuid.uuid4(), "failed"
        row.expires_at = previews.now() - timedelta(seconds=1)
        row.storage_ref = reference(row.project_id, row.id)
        vars(row).update(updates)
        return row

    missing = expired(storage_ref=None)
    wrong = expired()
    wrong.storage_ref["endpoint_url"] = "https://unrelated-storage.invalid"
    pending = expired(put_expires_at=previews.now() + timedelta(seconds=10))
    leased, failed, valid = expired(), expired(), expired()
    rows = [missing, wrong, pending, leased, failed, valid]
    db = SimpleNamespace(
        scalars=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
        scalar=AsyncMock(
            side_effect=[None, None, None, "other-node/preview-cpu", None, None]
        ),
        commit=AsyncMock(),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(
        previews,
        "lock_resource",
        AsyncMock(return_value=SimpleNamespace(preview_id=None)),
    )
    calls = []

    def delete(prefix, *, bucket):
        assert db.commit.await_count >= 2
        calls.append((bucket, prefix))
        if prefix == failed.storage_ref["prefix"]:
            raise OSError("temporary storage failure")

    monkeypatch.setattr(storage_service, "delete_prefix", delete)
    assert await previews.cleanup(db) == {"removed": 1, "stopping": 0}
    assert calls == [
        ("old-preview", row.storage_ref["prefix"]) for row in [failed, valid]
    ]
    db.delete.assert_awaited_once_with(valid)
    assert leased.status == "failed"
    assert failed.status == missing.status == "expired"
    assert owned.client.calls == []
