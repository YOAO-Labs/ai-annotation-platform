"""Pure Job/Bundle/message contracts and actual HTTP-handler wiring."""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sys
from types import ModuleType, SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest
from starlette.requests import Request

from app.schemas.async_job import AsyncJobOut
from app.schemas.export import ExportRequestBody, LidarExportOptions
from app.services import export_storage
from app.services.content_snapshot import ContentError
from app.services.exporting import lidar_jobs


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class JobDB:
    def __init__(self):
        self.events = []

    async def flush(self):
        self.events.append("flush")

    async def commit(self):
        self.events.append("commit")


class MediaStorageError(ValueError):
    code = "export_media_storage_identity_changed"
    status_code = 409


@pytest.fixture
def contract(monkeypatch):
    import app.services as services
    import app.services.exporting as exporting

    project = SimpleNamespace(id=uuid.uuid4(), data_type="lidar", display_id="P-LIDAR")
    batch_id = uuid.uuid4()
    opts = {"include_attributes": True, "lidar": {"kitti_camera_role": "camera_front"}}
    targets = ["pointmask", "kitti", "aap_json", "nuscenes"]
    snapshot = {
        "schema": 2,
        "project_id": str(project.id),
        "batch_id": str(batch_id),
        "targets": targets[:],
        "options": deepcopy(opts),
        "tasks": [{"id": str(uuid.uuid4()), "project_id": str(project.id)}],
    }
    owner = {
        "schema": 1,
        "purpose": "export",
        "location_id": str(uuid.uuid4()),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": "http://saved.test:9000",
        "bucket": "saved-export",
        "storage_identity": "saved-identity",
    }
    bundle = SimpleNamespace(
        id=uuid.uuid4(),
        project_id=project.id,
        job_id=uuid.uuid4(),
        zip_output_id=uuid.uuid4(),
        input_snapshot=snapshot,
        input_digest=digest(snapshot),
        storage_ref={**owner, "key_prefix": "lidar-export-media/objects/saved/"},
    )
    job = SimpleNamespace(
        id=bundle.job_id,
        project_id=project.id,
        kind="export",
        status="pending",
        payload={
            "targets": targets[:],
            "batch_id": str(batch_id),
            "output_authority": {"original_actor": str(uuid.uuid4())},
        },
    )
    job.payload["lidar_media"] = {
        "schema": 2,
        "bundle_id": str(bundle.id),
        "input_digest": bundle.input_digest,
    }
    state = SimpleNamespace(
        db=JobDB(),
        project=project,
        batch_id=batch_id,
        targets=targets,
        opts=opts,
        snapshot=snapshot,
        bundle=bundle,
        job=job,
        owner=owner,
        collected=[],
        allocated=[],
        zip_owners=[],
        logical={"pointmask": {"source.pcd": "a" * 64}},
    )

    async def collect(db, project_id, selected_batch, requested, options, *, lock):
        assert db is state.db and project_id == project.id and lock is True
        state.db.events.append("collect")
        state.collected.append((selected_batch, requested, options))
        snapshot.update(
            batch_id=str(selected_batch) if selected_batch else None,
            targets=requested[:],
            options=deepcopy(options),
        )
        return SimpleNamespace(snapshot=snapshot, digest=digest(snapshot))

    def logical_sources(frozen):
        assert frozen is snapshot
        state.db.events.append("paths")
        return state.logical

    async def allocate(db, **kwargs):
        assert db is state.db and kwargs["logical_sources"] is state.logical
        assert kwargs["input_snapshot"] is snapshot
        assert kwargs["input_digest"] == digest(snapshot)
        state.db.events.append("bundle")
        state.allocated.append(kwargs)
        bundle.input_digest = kwargs["input_digest"]
        return bundle

    async def allocate_zip(db, output):
        assert db is state.db
        state.db.events.append("zip-binding")
        state.zip_owners.append(output)
        return {**owner, "object_key": "reserved.zip"}

    for package, name, attributes in (
        (
            exporting,
            "lidar_inputs",
            {
                "uses_contract": lambda p, t: (
                    p.data_type == "lidar"
                    and bool(
                        {
                            "pointmask",
                            "kitti",
                            "coco-multicamera",
                            "nuscenes",
                        }.intersection(t)
                    )
                ),
                "collect": collect,
            },
        ),
        (exporting, "lidar_media_paths", {"logical_sources": logical_sources}),
        (services, "export_media_bundles", {"allocate": allocate}),
        (
            services,
            "export_media_storage",
            {"ExportMediaStorageError": MediaStorageError},
        ),
    ):
        module = ModuleType(package.__name__ + "." + name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, module.__name__, module)
        monkeypatch.setattr(package, name, module, raising=False)
    monkeypatch.setattr(export_storage, "allocate", allocate_zip)
    return state


def validate(state, **changes):
    return lidar_jobs.validate_message(
        state.job,
        state.bundle,
        **{
            "project_id": state.project.id,
            "batch_id": state.batch_id,
            "targets": state.targets,
            "opts": state.opts,
            **changes,
        },
    )


async def test_capture_flushes_only_small_proof_in_the_new_job_transaction(contract):
    state = contract
    del state.job.payload["lidar_media"]
    authority = state.job.payload["output_authority"]
    result = await lidar_jobs.capture_job(
        state.db,
        state.job,
        state.project,
        batch_id=state.batch_id,
        targets=state.targets,
        opts=state.opts,
    )
    assert result is state.bundle and state.job.payload["output_authority"] is authority
    assert set(state.job.payload) == {
        "targets",
        "batch_id",
        "output_authority",
        "lidar_media",
    }
    assert state.job.payload["batch_id"] == str(state.batch_id)
    assert state.job.payload["lidar_media"] == {
        "schema": 2,
        "bundle_id": str(state.bundle.id),
        "input_digest": state.bundle.input_digest,
    }
    assert state.collected[0][2] is state.opts
    assert state.db.events == ["collect", "paths", "bundle", "zip-binding", "flush"]
    reserved = state.zip_owners[0]
    assert (reserved.id, reserved.project_id, reserved.job_id, reserved.media) == (
        state.bundle.zip_output_id,
        state.project.id,
        state.job.id,
        "image",
    )
    assert validate(state) is None


@pytest.mark.parametrize(
    "data_type,targets",
    [("image", ["kitti"]), ("lidar", ["aap_json"]), ("video", ["kitti"])],
)
async def test_other_exports_are_unchanged_without_capture(
    contract, data_type, targets
):
    state = contract
    state.project.data_type = data_type
    before = deepcopy(state.job.payload)
    assert (
        await lidar_jobs.capture_job(
            state.db,
            state.job,
            state.project,
            batch_id=state.batch_id,
            targets=targets,
            opts=state.opts,
        )
        is None
    )
    assert state.job.payload == before and state.db.events == []


@pytest.mark.parametrize("field", sorted(lidar_jobs._OWNER_FIELDS))
async def test_capture_refuses_a_different_registered_zip_owner(contract, field):
    state = contract
    del state.job.payload["lidar_media"]
    state.bundle.storage_ref[field] = False if field.endswith("revision") else "changed"
    with pytest.raises(ContentError, match="lidar_export_storage_binding_changed"):
        await lidar_jobs.capture_job(
            state.db,
            state.job,
            state.project,
            batch_id=state.batch_id,
            targets=state.targets,
            opts=state.opts,
        )
    assert "lidar_media" not in state.job.payload and "commit" not in state.db.events


async def test_capture_never_recaptures_an_existing_private_proof(contract):
    state = contract
    before = deepcopy(state.job.payload)
    with pytest.raises(ContentError, match="lidar_export_contract_changed"):
        await lidar_jobs.capture_job(
            state.db,
            state.job,
            state.project,
            batch_id=state.batch_id,
            targets=state.targets,
            opts=state.opts,
        )
    assert state.job.payload == before and state.db.events == []


async def test_capture_maps_metadata_binding_errors_without_committing(
    contract, monkeypatch
):
    state = contract
    del state.job.payload["lidar_media"]

    async def unavailable(*args):
        raise export_storage.ExportStorageError("export_storage_binding_unproven")

    monkeypatch.setattr(export_storage, "allocate", unavailable)
    with pytest.raises(ContentError, match="export_storage_binding_unproven"):
        await lidar_jobs.capture_job(
            state.db,
            state.job,
            state.project,
            batch_id=state.batch_id,
            targets=state.targets,
            opts=state.opts,
        )
    assert "lidar_media" not in state.job.payload and "commit" not in state.db.events


async def test_capture_maps_bundle_owner_metadata_errors(contract, monkeypatch):
    from app.services import export_media_bundles

    state = contract
    del state.job.payload["lidar_media"]

    async def unavailable(*args, **kwargs):
        raise MediaStorageError()

    monkeypatch.setattr(export_media_bundles, "allocate", unavailable)
    with pytest.raises(ContentError, match="export_media_storage_identity_changed"):
        await lidar_jobs.capture_job(
            state.db,
            state.job,
            state.project,
            batch_id=state.batch_id,
            targets=state.targets,
            opts=state.opts,
        )
    assert "lidar_media" not in state.job.payload and "commit" not in state.db.events


@pytest.mark.parametrize(
    "change",
    [
        "kind",
        "job_id",
        "job_project",
        "message_project",
        "bundle_project",
        "bundle_id",
        "digest",
        "schema",
        "extra_proof",
        "missing_proof",
    ],
)
def test_messages_require_exact_job_bundle_and_private_proof(contract, change):
    state, changes = contract, {}
    proof = state.job.payload["lidar_media"]
    if change == "kind":
        state.job.kind = "training_dataset"
    elif change == "job_id":
        state.job.id = uuid.uuid4()
    elif change == "job_project":
        state.job.project_id = uuid.uuid4()
    elif change == "message_project":
        changes["project_id"] = uuid.uuid4()
    elif change == "bundle_project":
        state.bundle.project_id = uuid.uuid4()
    elif change == "bundle_id":
        proof["bundle_id"] = str(uuid.uuid4())
    elif change == "digest":
        proof["input_digest"] = "a" * 64
    elif change == "schema":
        proof["schema"] = True
    elif change == "extra_proof":
        proof["options"] = {}
    else:
        del state.job.payload["lidar_media"]
    with pytest.raises(ContentError):
        validate(state, **changes)


@pytest.mark.parametrize(
    "change",
    ["message", "durable", "missing_durable", "frozen", "missing_frozen", "project"],
)
def test_messages_keep_the_exact_project_or_batch_scope(contract, change):
    state, changes = contract, {}
    if change == "message":
        changes["batch_id"] = None
    elif change == "durable":
        state.job.payload["batch_id"] = None
    elif change == "missing_durable":
        del state.job.payload["batch_id"]
    elif change == "frozen":
        state.snapshot["batch_id"] = None
    elif change == "missing_frozen":
        del state.snapshot["batch_id"]
    else:
        state.snapshot["project_id"] = str(uuid.uuid4())
    with pytest.raises(ContentError, match="lidar_export_scope_changed"):
        validate(state, **changes)


@pytest.mark.parametrize(
    "change", ["order", "duplicate", "missing", "extra", "payload", "unknown"]
)
def test_messages_keep_clean_target_order_including_mixed_nuscenes(contract, change):
    state = contract
    assert validate(state) is None
    requested = state.targets[:]
    if change == "order":
        requested.reverse()
    elif change == "duplicate":
        requested.append(requested[0])
    elif change == "missing":
        requested.pop()
    elif change == "extra":
        requested.append("coco-multicamera")
    elif change == "payload":
        state.job.payload["targets"].reverse()
    else:
        requested.append("unknown")
    with pytest.raises(ContentError):
        validate(state, targets=requested)


@pytest.mark.parametrize(
    "change",
    ["bool", "float", "missing", "extra", "nested", "list", "tuple", "nan", "key"],
)
def test_options_are_exact_json_without_bool_numeric_coercion(contract, change):
    state = contract
    opts = deepcopy(state.opts)
    if change == "bool":
        opts["include_attributes"] = 1
    elif change == "float":
        opts["mots_frame_base"] = 0.0
        state.snapshot["options"]["mots_frame_base"] = 0
    elif change == "missing":
        del opts["lidar"]
    elif change == "extra":
        opts["cache_bypass"] = True
    elif change == "nested":
        opts["lidar"]["kitti_camera_role"] = "camera_back"
    elif change in {"list", "tuple"}:
        state.snapshot["options"]["values"] = [1]
        opts["values"] = [True] if change == "list" else (1,)
    elif change == "nan":
        state.snapshot["options"]["values"] = opts["values"] = float("nan")
    else:
        state.snapshot["options"]["values"] = opts["values"] = {1: "not-json"}
    with pytest.raises(ContentError, match="lidar_export_options_changed"):
        validate(state, opts=opts)


@pytest.mark.parametrize("task_ids", [[], [str(uuid.uuid4())]])
def test_selected_scene_task_deliveries_are_explicitly_rejected(contract, task_ids):
    with pytest.raises(
        ContentError, match="lidar_export_selected_scope_unsupported"
    ) as error:
        validate(contract, task_ids=task_ids)
    assert error.value.status == 422


def test_project_scope_and_reordered_json_keys_do_not_renew_proof(contract):
    state = contract
    state.snapshot["batch_id"] = state.job.payload["batch_id"] = None
    opts = dict(reversed(list(state.opts.items())))
    before = deepcopy(state.job.payload)
    assert validate(state, batch_id=None, opts=opts) is None
    assert state.job.payload == before and state.db.events == []


def test_public_job_schema_hides_private_proof_without_mutating_payload(contract):
    state = contract
    before = deepcopy(state.job.payload)
    now = datetime.now(timezone.utc)
    result = AsyncJobOut(
        id=state.job.id,
        kind="export",
        project_id=state.project.id,
        user_id=uuid.uuid4(),
        status="pending",
        progress_pct=0,
        payload=state.job.payload,
        result={},
        error_message=None,
        celery_task_id=None,
        started_at=None,
        completed_at=None,
        created_at=now,
        updated_at=now,
    )
    assert (
        "lidar_media" not in result.payload and "output_authority" not in result.payload
    )
    assert result.payload["batch_id"] == str(state.batch_id)
    assert state.job.payload == before


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("capture_failure", [False, True])
async def test_http_handlers_capture_before_audit_commit_and_dispatch_identical_opts(
    contract, monkeypatch, batch, capture_failure
):
    from app.api.v1 import batches, projects
    from app.services import async_job
    from app.services.audit import AuditService
    from app.services.exporting import (
        lidar_preflight,
        output_authority,
        reference_media,
    )
    from app.workers.export import run_export

    state = contract
    delivered = []
    output_proof = state.job.payload["output_authority"]
    reference_proof = {"original_reference_actor": str(uuid.uuid4())}

    async def preflight(*args, **kwargs):
        state.db.events.append("preflight")

    async def capture_output(*args, **kwargs):
        return output_proof

    async def capture_reference(*args, **kwargs):
        return reference_proof

    async def create_job(db, **kwargs):
        assert db is state.db
        state.db.events.append("create")
        state.job.payload = kwargs["payload"]
        return state.job

    async def audit(db, **kwargs):
        assert db is state.db
        state.db.events.append("audit")

    def dispatch(**kwargs):
        state.db.events.append("delay")
        delivered.append(kwargs)

    async def get_batch(self, selected):
        assert selected == state.batch_id
        return SimpleNamespace(project_id=state.project.id, display_id="B-LIDAR")

    if capture_failure:

        async def reject_capture(*args, **kwargs):
            state.db.events.append("capture-rejected")
            raise ContentError("lidar_export_inputs_busy")

        monkeypatch.setattr(lidar_jobs, "capture_job", reject_capture)
    monkeypatch.setattr(lidar_preflight, "assert_lidar_export_ready", preflight)
    monkeypatch.setattr(output_authority, "capture", capture_output)
    monkeypatch.setattr(reference_media, "capture_export_authority", capture_reference)
    monkeypatch.setattr(async_job, "create_job", create_job)
    monkeypatch.setattr(AuditService, "log", audit)
    monkeypatch.setattr(run_export, "delay", dispatch)
    monkeypatch.setattr(batches.BatchService, "get", get_batch)
    kwargs = {
        "request": Request({"type": "http", "headers": []}),
        "body": ExportRequestBody(
            lidar=LidarExportOptions(kitti_camera_role="camera_front")
        ),
        "targets": state.targets,
        "include_attributes": True,
        "video_frame_mode": "keyframes",
        "axis_frame": "iso",
        "video_overlap_policy": "error",
        "mots_frame_base": 0,
        "project": state.project,
        "actor": SimpleNamespace(id=uuid.uuid4()),
        "db": state.db,
    }
    if batch:
        handler = batches.export_batch
        kwargs.update(project_id=state.project.id, batch_id=state.batch_id)
    else:
        handler = projects.export_project
        kwargs["indexed_overlap_policy"] = "error"
    if capture_failure:
        with pytest.raises(HTTPException) as error:
            await handler(**kwargs)
        assert error.value.status_code == 409
        assert error.value.detail == {"code": "lidar_export_inputs_busy"}
        assert state.db.events == ["preflight", "create", "capture-rejected"]
        assert not delivered
        return
    result = await handler(**kwargs)
    assert result == {"job_id": str(state.job.id)}
    assert state.db.events == [
        "preflight",
        "create",
        "collect",
        "paths",
        "bundle",
        "zip-binding",
        "flush",
        "audit",
        "commit",
        "delay",
    ]
    assert delivered[0]["opts"] is state.collected[0][2]
    assert state.job.payload["batch_id"] == (str(state.batch_id) if batch else None)
    assert delivered[0]["batch_id"] == state.job.payload["batch_id"]
    assert state.job.payload["output_authority"] is output_proof
    assert state.job.payload["export_authority"] is reference_proof


async def test_pure_nuscenes_gets_new_contract_in_http_transaction(contract):
    state = contract
    del state.job.payload["lidar_media"]
    state.job.payload["targets"] = ["nuscenes"]
    bundle = await lidar_jobs.capture_job(
        state.db,
        state.job,
        state.project,
        batch_id=state.batch_id,
        targets=["nuscenes"],
        opts=state.opts,
    )
    assert bundle is state.bundle
    assert state.collected[0][1] == ["nuscenes"]
    assert state.snapshot["schema"] == 2
    assert state.job.payload["lidar_media"]["schema"] == 2
    assert "commit" not in state.db.events


@pytest.mark.parametrize("old", ["proof", "snapshot"])
def test_old_partial_contract_cannot_start_a_new_export(contract, old):
    state = contract
    if old == "proof":
        state.job.payload["lidar_media"]["schema"] = 1
    else:
        state.snapshot["schema"] = 1
    with pytest.raises(ContentError):
        validate(state)
