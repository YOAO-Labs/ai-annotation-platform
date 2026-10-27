from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock

import pytest

from aap_assets_inputs import contract as c
from app.application.preannotation_contract import BatchPredictionRequest
from app.db.models.async_job import AsyncJob
from app.application.preannotation_execution import (
    PreannotationInputDeferred,
    _apply_prediction_result_bundle_with_retries,
    _apply_prediction_result_with_retries,
    _prepare_accepted_input,
    _prepare_accepted_input_manifest,
)
from app.services.labeling_prediction_contract import ApplyPredictionResultError
from app.services import async_job as async_job_svc


@pytest.fixture(autouse=True)
def _bind_jobs_read_to_fake_session(monkeypatch):
    async def get_job(db, job_id, *, populate_existing=False):
        return await db.get(AsyncJob, job_id, populate_existing=populate_existing)

    monkeypatch.setattr(async_job_svc, "get_job", get_job)


class _Session:
    autoflush = True
    identity_map: dict = {}

    def __init__(self, job, task):
        self.job, self.task = job, task

    async def execute(self, _statement):
        task = self.task
        return SimpleNamespace(scalar_one_or_none=lambda: task)

    async def flush(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, model, _identity, **_kwargs):
        if model.__name__ == "AsyncJob":
            return self.job
        if model.__name__ == "Task":
            return self.task
        return None


def _request(*, protocol=c.INPUT_SCHEMA, input_ids=None, authority_kind=None):
    job_id = uuid.uuid4()
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    input_id = uuid.uuid4()
    mapping = input_ids if input_ids is not None else {str(task_id): str(input_id)}
    payload = {
        "execution_scope": "workbench",
        "task_ids": [str(task_id)],
        "scope": {"task_ids": [str(task_id)]},
        "accepted_inputs_protocol": protocol,
        "accepted_input_ids": mapping,
    }
    if authority_kind is not None:
        binding = {"task_id": str(task_id)}
        if authority_kind in {"reference", "both"}:
            binding["reference"] = {"sha256": "a" * 64}
        if authority_kind in {"original", "both"}:
            binding["original"] = {"sha256": "a" * 64}
        payload["prediction_authority"] = {"schema": 2, "tasks": [binding]}
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(uuid.uuid4()),
        task_ids=[str(task_id)],
        execution_scope="workbench",
        async_job_id=str(job_id),
        user_id=str(actor_id),
        accepted_inputs_protocol=protocol,
        accepted_input_ids=mapping if protocol is not None else None,
    )
    job = SimpleNamespace(
        status="pending",
        kind="batch_predict",
        project_id=project_id,
        user_id=actor_id,
        payload=payload,
    )
    task = SimpleNamespace(
        id=task_id,
        project_id=project_id,
        file_type="image",
        version=17,
    )
    return request, job, task, input_id


def _session_factory(job, task):
    return lambda: _Session(job, task)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_job_id", [False, True])
@pytest.mark.parametrize("scene_mode", [False, True])
async def test_bulk_image_without_manifest_identity_fails_closed(
    with_job_id, scene_mode, monkeypatch
):
    from app.db.models.async_job import AsyncJobStatus

    project_id = uuid.uuid4()
    job_id = uuid.uuid4() if with_job_id else None
    actor_id = uuid.uuid4()
    project = SimpleNamespace(data_type="image", scene_mode=scene_mode)
    monkeypatch.setattr(
        "app.application.preannotation_execution.get_workspace_project",
        AsyncMock(return_value=project),
    )
    job = SimpleNamespace(
        kind="batch_predict",
        project_id=project_id,
        user_id=actor_id,
        status=AsyncJobStatus.PENDING.value,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _identity, **_kwargs):
            if model.__name__ == "Project":
                return project
            if model.__name__ == "AsyncJob" and with_job_id:
                return job
            return None

    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(uuid.uuid4()),
        user_id=str(actor_id),
        execution_scope="bulk",
        async_job_id=str(job_id) if job_id else None,
    )

    with pytest.raises(ValueError, match="no frozen input manifest identity"):
        async with _prepare_accepted_input_manifest(request, lambda: Session()):
            pytest.fail("bulk image work must not fall back to current Task URLs")


@pytest.mark.asyncio
@pytest.mark.parametrize("scene_mode", [False, True])
async def test_running_legacy_bulk_image_job_is_drained(monkeypatch, scene_mode):
    project_id, job_id, actor_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    project = SimpleNamespace(data_type="image", scene_mode=scene_mode)
    monkeypatch.setattr(
        "app.application.preannotation_execution.get_workspace_project",
        AsyncMock(return_value=project),
    )
    job = AsyncJob(
        id=job_id,
        kind="batch_predict",
        project_id=project_id,
        user_id=actor_id,
        status="running",
        payload={"execution_scope": "bulk"},
        completed_at=None,
        error_message=None,
    )

    class Session:
        commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _identity, **_kwargs):
            if model.__name__ == "Project":
                return project
            if model.__name__ == "AsyncJob":
                return job
            return None

        async def commit(self):
            self.commits += 1

    session = Session()

    async def no_notify(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.services.async_job_notify.notify_job_terminal", no_notify)
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(uuid.uuid4()),
        user_id=str(actor_id),
        execution_scope="bulk",
        async_job_id=str(job_id),
    )

    async with _prepare_accepted_input_manifest(request, lambda: session):
        pass

    assert job.status == "failed"
    assert job.error_message == ("legacy_bulk_image_job_requires_frozen_input_manifest")
    assert session.commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
        "manifest_not_ready",
        "input_not_ready",
        "reference_busy",
        "source_busy",
        "job_busy",
        "account_busy",
        "task_busy",
        "storage_unavailable",
    ],
)
async def test_unsealed_manifest_defers_before_job_claim(monkeypatch, code):
    from contextlib import asynccontextmanager
    from types import ModuleType
    import sys

    from aap_assets_input_manifests import contract as manifests

    job_id, actor_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    digest = "f" * 64
    selection = manifests.ManifestSelection(
        batch_id=None, task_ids=None, predict_mode="append"
    )
    job = SimpleNamespace(
        status="pending",
        kind="batch_predict",
        project_id=project_id,
        user_id=actor_id,
        payload={
            "execution_scope": "bulk",
            "batch_id": None,
            "input_selection": selection.model_dump(mode="json"),
            "accepted_input_manifest_protocol": manifests.MANIFEST_SCHEMA,
            "accepted_input_manifest_id": str(job_id),
            "accepted_input_manifest_digest": digest,
            "total_tasks": 1,
            "input_candidate_count": 1,
        },
    )
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(uuid.uuid4()),
        task_ids=None,
        user_id=str(actor_id),
        execution_scope="bulk",
        predict_mode="append",
        async_job_id=str(job_id),
        accepted_input_manifest_protocol=manifests.MANIFEST_SCHEMA,
        accepted_input_manifest_id=str(job_id),
        accepted_input_manifest_digest=digest,
    )

    class Gateway:
        async def ensure_sealed(self, _scope, *, selection_digest):
            assert selection_digest == digest
            raise manifests.ManifestFailure(code)

    @asynccontextmanager
    async def gateway(**_kwargs):
        yield Gateway()

    bridge = ModuleType("app.services.accepted_input_manifests")
    bridge.input_manifest_gateway = gateway
    monkeypatch.setitem(sys.modules, bridge.__name__, bridge)

    with pytest.raises(PreannotationInputDeferred, match=code):
        async with _prepare_accepted_input_manifest(
            request, _session_factory(job, None)
        ):
            pytest.fail("an unsealed manifest cannot claim the durable Job")

    assert job.status == "pending"


@pytest.mark.asyncio
async def test_bulk_text_stage_is_outside_image_manifest_drain_guard():
    project_id = uuid.uuid4()
    request = BatchPredictionRequest(
        project_id=str(project_id),
        ml_backend_id=str(uuid.uuid4()),
        execution_scope="bulk",
        pipeline_stages=[{"stage": 1, "task": "text"}],
    )

    async with _prepare_accepted_input_manifest(request, lambda: pytest.fail()):
        pass


@pytest.mark.asyncio
async def test_explicit_scene_unit_is_outside_ordinary_manifest_drain_guard():
    request = BatchPredictionRequest(
        project_id=str(uuid.uuid4()),
        ml_backend_id=str(uuid.uuid4()),
        execution_scope="bulk",
        execution_unit="scene",
    )
    async with _prepare_accepted_input_manifest(
        request, lambda: pytest.fail("Scene work is not an ordinary image Job")
    ) as prepared:
        assert prepared is None


@pytest.mark.asyncio
async def test_missing_legacy_workbench_image_identity_fails_closed(monkeypatch):
    request, job, task, _input_id = _request(protocol=None)
    job.payload.pop("accepted_inputs_protocol")
    job.payload.pop("accepted_input_ids")
    called = False

    @asynccontextmanager
    async def fake_input_gateway(**_kwargs):
        nonlocal called
        called = True
        yield None

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", fake_input_gateway
    )
    with pytest.raises(ValueError, match="legacy Workbench image"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pass
    assert not called
    assert job.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("authority_kind", ["original", "both"])
async def test_workbench_image_without_identity_stays_closed(
    monkeypatch, authority_kind
):
    request, job, task, _input_id = _request(
        protocol=None, authority_kind=authority_kind
    )
    job.payload.pop("accepted_inputs_protocol")
    job.payload.pop("accepted_input_ids")

    @asynccontextmanager
    async def unexpected_input_gateway(**_kwargs):
        pytest.fail("non-reference workbench images require admitted identity")
        yield None

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", unexpected_input_gateway
    )
    with pytest.raises(ValueError, match="legacy Workbench image"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pass
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_frozen_reference_replaces_admission_for_workbench_image(monkeypatch):
    request, job, task, _input_id = _request(protocol=None, authority_kind="reference")
    job.payload.pop("accepted_inputs_protocol")
    job.payload.pop("accepted_input_ids")

    @asynccontextmanager
    async def unexpected_input_gateway(**_kwargs):
        pytest.fail("reference originals are owned by their frozen authority")
        yield None

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", unexpected_input_gateway
    )
    async with _prepare_accepted_input(
        request, _session_factory(job, task)
    ) as prepared:
        assert prepared is None
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_remote_pending_input_defers_before_worker_claim(monkeypatch):
    request, job, task, _input_id = _request()

    class Gateway:
        async def ensure_reference(self, _request):
            raise c.InputFailure("input_not_ready")

    @asynccontextmanager
    async def fake_input_gateway(**_kwargs):
        yield Gateway()

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", fake_input_gateway
    )
    with pytest.raises(PreannotationInputDeferred, match="input_not_ready"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pytest.fail("pending input must not reach the inference workflow")
    assert job.status == "pending"


@pytest.mark.asyncio
async def test_ready_input_is_downloaded_to_a_private_path_and_keeps_receipt(
    monkeypatch,
):
    request, job, task, input_id = _request()
    input_record = SimpleNamespace(
        input_id=input_id,
        job_id=uuid.UUID(request.async_job_id),
        task_id=task.id,
        project_id=task.project_id,
        actor_id=uuid.UUID(request.user_id),
        task_version=task.version,
        sha256="a" * 64,
        byte_size=4,
    )
    reference = SimpleNamespace(
        input=input_record, url="https://inputs.test/source?versionId=v1"
    )
    downloaded = []

    class Gateway:
        async def ensure_reference(self, _request):
            return reference

        async def download(self, _reference, destination):
            destination.write_bytes(b"data")
            destination.chmod(0o400)
            downloaded.append(destination)

        async def verify_source(self, _request):
            return input_record

    @asynccontextmanager
    async def fake_input_gateway(**_kwargs):
        yield Gateway()

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", fake_input_gateway
    )
    async with _prepare_accepted_input(
        request, _session_factory(job, task)
    ) as prepared:
        assert prepared.reference.url.endswith("versionId=v1")
        assert prepared.local_path == downloaded[0]
        assert prepared.local_path.read_bytes() == b"data"
        assert prepared.local_path.stat().st_mode & 0o777 == 0o400
        assert prepared.local_path.parent.stat().st_mode & 0o777 == 0o700
        assert await prepared.verify_source() is input_record
    assert not prepared.local_path.exists()


@pytest.mark.asyncio
async def test_request_input_map_must_match_durable_job_scope(monkeypatch):
    request, job, task, _input_id = _request()
    job.payload["accepted_input_ids"] = {str(task.id): str(uuid.uuid4())}

    @asynccontextmanager
    async def unexpected_input_gateway(**_kwargs):
        pytest.fail("durable-map mismatch must fail before input access")
        yield None

    monkeypatch.setattr(
        "app.services.accepted_inputs.input_gateway", unexpected_input_gateway
    )
    with pytest.raises(ValueError, match="differs from durable job scope"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["source_changed", "storage_unavailable"])
async def test_source_verification_precedes_job_claim_and_overwrite(monkeypatch, code):
    request, job, task, input_id = _request()
    record = SimpleNamespace(
        input_id=input_id,
        job_id=uuid.UUID(request.async_job_id),
        task_id=task.id,
        project_id=task.project_id,
        actor_id=uuid.UUID(request.user_id),
        task_version=task.version,
        sha256="a" * 64,
        byte_size=4,
    )
    paths = []

    class Gateway:
        async def ensure_reference(self, _request):
            return SimpleNamespace(input=record)

        async def download(self, _reference, destination):
            destination.write_bytes(b"data")
            paths.append(destination)

        async def verify_source(self, _request):
            assert paths[0].is_file()
            raise c.InputFailure(code)

    @asynccontextmanager
    async def gateway(**_kwargs):
        yield Gateway()

    async def no_sleep(_delay):
        pass

    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", gateway)
    monkeypatch.setattr(
        "app.application.preannotation_execution.asyncio.sleep", no_sleep
    )
    failure = c.InputFailure if code == "source_changed" else PreannotationInputDeferred
    with pytest.raises(failure, match=code):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pytest.fail("invalid input must not reach job claim or overwrite")
    assert job.status == "pending"
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_private_workspace_failure_defers_before_job_claim(monkeypatch):
    request, job, task, input_id = _request()
    record = SimpleNamespace(
        input_id=input_id,
        job_id=uuid.UUID(request.async_job_id),
        task_id=task.id,
        project_id=task.project_id,
        actor_id=uuid.UUID(request.user_id),
        task_version=task.version,
    )

    class Gateway:
        async def ensure_reference(self, _request):
            return SimpleNamespace(input=record)

        async def download(self, *_args):
            pytest.fail("unavailable private workspace must precede transfer")

    @asynccontextmanager
    async def gateway(**_kwargs):
        yield Gateway()

    def unavailable(**_kwargs):
        raise OSError("private temporary storage unavailable")

    monkeypatch.setattr("app.services.accepted_inputs.input_gateway", gateway)
    monkeypatch.setattr(
        "app.application.preannotation_execution.tempfile.TemporaryDirectory",
        unavailable,
    )
    with pytest.raises(PreannotationInputDeferred, match="storage_unavailable"):
        async with _prepare_accepted_input(request, _session_factory(job, task)):
            pytest.fail("unavailable workspace must not reach job claim")
    assert job.status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["input_busy", "mask_content_deleting", "mask_content_epoch_changed"],
)
async def test_busy_write_retries_the_same_model_result_after_rollback(
    monkeypatch, code
):
    request = SimpleNamespace(result=[{"class_name": "car"}])
    calls = []

    class Writer:
        async def apply_prediction_result(self, received):
            calls.append(received)
            if len(calls) < 3:
                raise ApplyPredictionResultError(
                    code=code,
                    status_code=409,
                    detail={"reason": code},
                )
            return "saved"

    class Session:
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    async def no_sleep(_delay):
        return None

    import app.application.preannotation_execution as execution

    monkeypatch.setattr(execution.asyncio, "sleep", no_sleep)
    session = Session()
    result = await _apply_prediction_result_with_retries(Writer(), session, request)

    assert result == "saved"
    assert calls == [request, request, request]
    assert all(received is request for received in calls)
    assert session.rollbacks == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["input_busy", "mask_content_deleting", "mask_content_epoch_changed"],
)
async def test_busy_bundle_write_retries_the_whole_same_result_tuple(monkeypatch, code):
    requests = (
        SimpleNamespace(result=[{"class_name": "car", "index": 1}]),
        SimpleNamespace(result=[{"class_name": "car", "index": 2}]),
    )
    calls = []

    class Writer:
        async def apply_prediction_result_bundle(self, received):
            calls.append(received)
            if len(calls) == 1:
                received[0].result[0]["class_name"] = "mutated by failed attempt"
                raise ApplyPredictionResultError(
                    code=code,
                    status_code=409,
                    detail={"reason": code},
                )
            return ("first", "second")

    class Session:
        rollbacks = 0

        async def rollback(self):
            self.rollbacks += 1

    async def no_sleep(_delay):
        return None

    import app.application.preannotation_execution as execution

    monkeypatch.setattr(execution.asyncio, "sleep", no_sleep)
    session = Session()
    result = await _apply_prediction_result_bundle_with_retries(
        Writer(), session, requests
    )

    assert result == ("first", "second")
    assert len(calls) == 2
    assert calls[0] is not calls[1]
    assert calls[1][0].result == [{"class_name": "car", "index": 1}]
    assert calls[1][1].result == [{"class_name": "car", "index": 2}]
    assert requests[0].result == [{"class_name": "car", "index": 1}]
    assert session.rollbacks == 1


@pytest.mark.asyncio
async def test_bundle_write_rejects_target_scope_mismatch_before_mask_io():
    from app.services.labeling_prediction_contract import ApplyPredictionResultRequest
    from app.services.prediction import PredictionService

    task_id, other_task_id, project_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    scope = SimpleNamespace(task_id=task_id, project_id=project_id)
    request = ApplyPredictionResultRequest(
        task_id=other_task_id,
        project_id=project_id,
        ml_backend_id=None,
        result=[{"type": "bbox", "class_name": "car"}],
        write_scope=scope,
    )

    class Session:
        async def get(self, *_args, **_kwargs):
            pytest.fail("bundle scope must be checked before mask preflight")

    with pytest.raises(ValueError, match="write scope does not match its target"):
        await PredictionService(Session()).apply_prediction_result_bundle((request,))


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["manifest", "bulk", "workbench"])
async def test_manifest_failed_prediction_without_input_cannot_resolve_current_url(
    origin,
):
    from app.workers.predictions_retry import _prepare_retry_input

    failed_id = uuid.uuid4()
    task_id, project_id, backend_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    task = SimpleNamespace(id=task_id, file_type="image")
    backend = SimpleNamespace(id=backend_id)
    failed = SimpleNamespace(
        task_id=task_id,
        project_id=project_id,
        ml_backend_id=backend_id,
        extra={
            "execution_scope": "workbench" if origin == "workbench" else "bulk",
            **(
                {
                    "accepted_input_manifest": {
                        "protocol": "aap-assets-input-manifest/v1",
                        "manifest_id": str(uuid.uuid4()),
                        "ordinal": 4,
                    }
                }
                if origin == "manifest"
                else {}
            ),
        },
    )

    class Session:
        autoflush = True
        identity_map: dict = {}

        def __init__(self):
            self._rows = [failed, task]

        async def execute(self, _statement):
            row = self._rows.pop(0) if self._rows else task
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def flush(self):
            return None

        async def scalar(self, *_args, **_kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, model, _identity):
            if model.__name__ == "FailedPrediction":
                return failed
            if model.__name__ == "Task":
                return task
            if model.__name__ == "MLBackendRegistry":
                return backend
            return None

    async with _prepare_retry_input(lambda: Session(), str(failed_id)) as prepared:
        assert prepared.accepted_input is None
        assert prepared.terminal_error is not None
        assert "accepted input" in str(prepared.terminal_error)
        assert prepared.gateway is None
