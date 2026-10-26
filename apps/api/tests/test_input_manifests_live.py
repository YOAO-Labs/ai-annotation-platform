"""Real manifest HTTP, restricted materializer, retained bytes and Node peer."""

import asyncio
from contextlib import asynccontextmanager
from io import BytesIO
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import httpx
import pytest
from PIL import Image
from sqlalchemy import select, text

from aap_assets_input_manifests import contract as c
from aap_assets_input_manifests.client import ManifestClient
from aap_assets_inputs import contract as inputs
from aap_assets_inputs.client import InputClient
from app.db.models.async_job import AsyncJob
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset, Scene
from app.db.models.task import Task
from app.services.accepted_input_manifests import get_input_manifest_capture
from tests.factory import create_batch, create_project, create_task
from tests.test_assets_catalog_native import _seed_actor
from tests.test_assets_images_live import _image_live_runtime, _seed_item, _solid_png
from tests.test_assets_media_live import _process_log, _wait_process_exit

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data PostgreSQL and scoped S3 verifier",
    ),
]


@pytest.fixture(autouse=True)
def installed_runtime_processes(monkeypatch):
    executable = os.environ.get("AAP_INPUT_MANIFEST_RUNTIME_PYTHON")
    if not executable:
        yield
        return
    from tests import test_assets_images_live as images

    runtime_python = Path(executable)
    assert runtime_python.is_absolute() and runtime_python.is_file()
    start = images._start_process
    started = []

    def installed(command, environment, log_path):
        module = command[2] if command[1:2] == ["-m"] else None
        assert module in {
            "aap_assets_media_runtime",
            "aap_assets_images_runtime.worker",
        }
        started.append(module)
        isolated_environment = {
            key: value for key, value in environment.items() if key != "PYTHONPATH"
        }
        return start(
            ["rtk", "proxy", str(runtime_python), "-I", *command[1:]],
            isolated_environment,
            log_path,
        )

    monkeypatch.setattr(images, "_start_process", installed)
    yield
    assert "aap_assets_media_runtime" in started


class _DropPreparedResponse(httpx.AsyncBaseTransport):
    def __init__(self):
        self.inner = httpx.AsyncHTTPTransport(trust_env=False)
        self.dropped = False

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        if (
            request.url.path == "/v1/input-manifests/prepare"
            and response.status_code == 200
            and not self.dropped
        ):
            await response.aread()
            await response.aclose()
            self.dropped = True
            raise httpx.ReadError("injected manifest response loss", request=request)
        return response

    async def aclose(self):
        await self.inner.aclose()


@asynccontextmanager
async def _manifest_runtime(test_engine, tmp_path):
    async with _image_live_runtime(test_engine, tmp_path) as runtime:
        try:
            yield runtime
        finally:
            if runtime.project_ids:
                async with runtime.owner_sessions.begin() as owner:
                    parameters = {"projects": runtime.project_ids}
                    await owner.execute(
                        text(
                            "DELETE FROM public.prediction_metas WHERE prediction_id IN "
                            "(SELECT id FROM public.predictions WHERE project_id=ANY(CAST(:projects AS uuid[]))) "
                            "OR failed_prediction_id IN (SELECT id FROM public.failed_predictions "
                            "WHERE project_id=ANY(CAST(:projects AS uuid[])))"
                        ),
                        parameters,
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.failed_predictions WHERE project_id=ANY(CAST(:projects AS uuid[]))"
                        ),
                        parameters,
                    )
                    await owner.execute(
                        text(
                            "DELETE FROM public.predictions WHERE project_id=ANY(CAST(:projects AS uuid[]))"
                        ),
                        parameters,
                    )


async def _capture(
    runtime, actor_id, project_id, *, selection=None, extra_payload=None
):
    job_id = uuid4()
    selection = selection or c.ManifestSelection(predict_mode="overwrite")
    async with runtime.owner_sessions.begin() as owner:
        job = AsyncJob(
            id=job_id,
            kind="batch_predict",
            user_id=actor_id,
            project_id=project_id,
            status="pending",
            celery_task_id=str(job_id),
            result={},
            payload={
                "execution_scope": "bulk",
                "batch_id": str(selection.batch_id) if selection.batch_id else None,
                "accepted_input_manifest_protocol": c.MANIFEST_SCHEMA,
                "accepted_input_manifest_id": str(job_id),
                "input_selection": selection.model_dump(mode="json"),
                **(
                    {
                        "task_ids": [str(task) for task in selection.task_ids],
                        "scope": {
                            "task_ids": [str(task) for task in selection.task_ids]
                        },
                    }
                    if selection.task_ids
                    else {}
                ),
                **(extra_payload or {}),
            },
        )
        owner.add(job)
        await owner.flush()
        header = await get_input_manifest_capture()(owner).capture(
            c.CaptureManifestRequest(
                job_id=job_id,
                actor_id=actor_id,
                project_id=project_id,
                selection=selection,
            )
        )
        job.payload = {
            **job.payload,
            "accepted_input_manifest_digest": header.selection_digest,
            "total_tasks": header.selected_count,
            "input_candidate_count": header.candidate_count,
        }
    return header


async def _node_peer(runtime, header):
    root = Path(__file__).resolve().parents[3]
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "rtk",
            "proxy",
            "node",
            str(root / "apps/assets-media/tests/fixtures/input-manifests-peer.mjs"),
        ],
        input=json.dumps(
            {
                "origin": str(runtime.control_client.base_url).rstrip("/"),
                "token": runtime.control_token,
                **header.model_dump(
                    mode="json",
                    include={
                        "job_id",
                        "actor_id",
                        "project_id",
                        "selection_digest",
                        "sealed_digest",
                        "selected_count",
                    },
                ),
            }
        ),
        text=True,
        capture_output=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", "")},
        check=False,
    )
    assert result.returncode == 0, (
        "independent Node manifest consumer rejected contract"
    )
    assert json.loads(result.stdout) == {
        "schema": c.MANIFEST_SCHEMA,
        "job_id": str(header.job_id),
        "selection_digest": header.selection_digest,
        "sealed_digest": header.sealed_digest,
        "selected_count": header.selected_count,
        "pages": max(1, (header.selected_count + 99) // 100),
    }


@pytest.mark.parametrize("replace_source", [False, True])
async def test_manifest_response_loss_replays_one_input_in_restricted_materializer(
    test_engine, tmp_path, caplog, replace_source
):
    caplog.set_level(logging.WARNING, logger="httpx")
    async with _manifest_runtime(test_engine, tmp_path) as runtime:
        original = _solid_png((128, 96), (240, 20, 30))
        source_key = f"image-source/{uuid4()}/manifest.png"
        async with runtime.owner_sessions.begin() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
        runtime.track_actor(actor_id)
        dataset_id, item_id = await _seed_item(
            runtime,
            actor_id=actor_id,
            source_key=source_key,
            source_bytes=original,
            width=128,
            height=96,
        )
        async with runtime.owner_sessions.begin() as owner:
            project = await create_project(owner, owner_id=actor_id)
            runtime.project_ids.append(project.id)
            owner.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
            task = await create_task(owner, project_id=project.id)
            task.dataset_item_id, task.file_path = item_id, source_key
            runtime.task_ids.append(task.id)
        header = await _capture(runtime, actor_id, project.id)
        scope = c.ManifestScope(
            job_id=header.job_id, actor_id=actor_id, project_id=project.id
        )
        page_request = c.ManifestPageRequest(
            **scope.model_dump(), selection_digest=header.selection_digest
        )
        preparation = c.PrepareManifestMemberRequest(
            **scope.model_dump(),
            selection_digest=header.selection_digest,
            ordinal=1,
            task_id=task.id,
        )
        origin = str(runtime.control_client.base_url).rstrip("/")
        transport = _DropPreparedResponse()
        async with httpx.AsyncClient(
            transport=transport, timeout=10, trust_env=False
        ) as http:
            manifests = ManifestClient(origin, runtime.control_token, client=http)
            original_inputs = InputClient(origin, runtime.control_token, client=http)
            assert (await manifests.page(page_request)).members[0].input is None
            # The generic accept route cannot create a new bulk Input outside
            # its immutable member's preparation capability.
            with pytest.raises(inputs.InputFailure):
                await original_inputs.accept(
                    inputs.FindInputRequest(
                        job_id=header.job_id, task_id=task.id, actor_id=actor_id
                    )
                )
            assert (await manifests.page(page_request)).members[0].input is None
            with pytest.raises(c.ManifestFailure, match="storage_unavailable"):
                await manifests.prepare(preparation)
            assert transport.dropped
            accepted = (await manifests.page(page_request)).members[0].input
            assert accepted is not None and accepted.status == "pending"
            if replace_source:
                await asyncio.to_thread(
                    runtime.owner_s3.put_object,
                    Bucket=runtime.datasets_bucket,
                    Key=source_key,
                    Body=b"replacement source",
                )
            replayed = await manifests.prepare(preparation)
            assert replayed.member.input.input_id == accepted.input_id
            assert replayed.member.input.source_snapshot == accepted.source_snapshot
            process, log = runtime.start_worker(
                "manifest-materializer", input_id=accepted.input_id
            )
            code = await _wait_process_exit(
                process,
                timeout=45,
                message="manifest input materialization did not finish",
            )
            assert code == 0, _process_log(log)
            sealed = await manifests.seal(
                c.SealManifestRequest(
                    **scope.model_dump(), selection_digest=header.selection_digest
                )
            )
            assert sealed.selected_count == 1 and sealed.prepared_count == 1
            assert sealed.ready_count == (0 if replace_source else 1)
            assert sealed.failed_count == (1 if replace_source else 0)
            sealed_page = await manifests.page(
                page_request.model_copy(update={"sealed_digest": sealed.sealed_digest})
            )
            assert sealed_page.members[0].input.input_id == accepted.input_id
            if replace_source:
                assert sealed_page.members[0].state == "failed"
                assert sealed_page.members[0].error == "source_changed"
            else:
                request = inputs.InputRequest(
                    input_id=accepted.input_id, job_id=header.job_id, task_id=task.id
                )
                reference = await original_inputs.reference(request)
                assert reference.input.sha256 == sha256(original).hexdigest()
                await original_inputs.download(
                    reference, tmp_path / "manifest-original.png"
                )
                assert (tmp_path / "manifest-original.png").read_bytes() == original
            await _node_peer(runtime, sealed)


@pytest.mark.parametrize(
    "scene_mode",
    [pytest.param(False, id="ordinary"), pytest.param(True, id="scene")],
)
async def test_bulk_consumer_retains_original_inputs_for_roi_partial_failure_and_retry(
    test_engine, tmp_path, caplog, monkeypatch, scene_mode
):
    from app.application.preannotation_contract import BatchPredictionRequest
    from app.application.preannotation_execution import (
        PreannotationInputDeferred,
        run_preannotation,
    )
    from app.config import settings
    from app.db.models.ml_backend_registry import MLBackendRegistry
    from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
    from app.workers.predictions_retry import _do_retry_with_factory
    from tests.test_staged_pipeline_worker import _bbox

    caplog.set_level(logging.WARNING, logger="httpx")
    async with _manifest_runtime(test_engine, tmp_path) as runtime:
        async with runtime.owner_sessions.begin() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
            project = await create_project(owner, owner_id=actor_id)
            project.scene_mode = scene_mode
            batch = await create_batch(owner, project_id=project.id, status="active")
            backend = MLBackendRegistry(
                name=f"manifest-{uuid4()}",
                url=f"http://unused-{uuid4().hex}.test/",
                state="connected",
                is_interactive=True,
            )
            owner.add(backend)
            await owner.flush()
        runtime.track_actor(actor_id)
        runtime.project_ids.append(project.id)
        original = _solid_png((128, 96), (240, 20, 30))
        tasks, source_keys = [], []
        for index in range(3):
            source_key = f"image-source/{uuid4()}/bulk-{index}.png"
            dataset_id, item_id = await _seed_item(
                runtime,
                actor_id=actor_id,
                source_key=source_key,
                source_bytes=original,
                width=128,
                height=96,
            )
            async with runtime.owner_sessions.begin() as owner:
                owner.add(ProjectDataset(project_id=project.id, dataset_id=dataset_id))
                if scene_mode:
                    dataset = await owner.get(Dataset, dataset_id)
                    dataset.is_temporal = True
                    scene = Scene(
                        display_id=f"S-{uuid4().hex[:12]}",
                        dataset_id=dataset_id,
                        name="sequence",
                        created_by=actor_id,
                    )
                    owner.add(scene)
                    await owner.flush()
                    item = await owner.get(DatasetItem, item_id)
                    item.scene_id = scene.id
                    item.frame_index = 0
                task = await create_task(owner, project_id=project.id)
                task.batch_id, task.dataset_item_id, task.file_path = (
                    batch.id,
                    item_id,
                    source_key,
                )
                owner.add(
                    Prediction(
                        task_id=task.id,
                        project_id=project.id,
                        ml_backend_id=backend.id,
                        result=[_bbox(10, 10, 20, 20)],
                        score=0.5,
                        tool_unit_id="bbox",
                    )
                )
                task.total_predictions = 1
            tasks.append(task)
            source_keys.append(source_key)
            runtime.task_ids.append(task.id)
        selection = c.ManifestSelection(batch_id=batch.id, predict_mode="overwrite")
        header = await _capture(
            runtime,
            actor_id,
            project.id,
            selection=selection,
            extra_payload={"ml_backend_id": str(backend.id)},
        )
        scope = c.ManifestScope(
            job_id=header.job_id, actor_id=actor_id, project_id=project.id
        )
        origin = str(runtime.control_client.base_url).rstrip("/")
        monkeypatch.setattr(settings, "assets_media_url", origin)
        monkeypatch.setattr(settings, "assets_media_token", runtime.control_token)
        monkeypatch.setattr(
            settings,
            "database_url",
            test_engine.url.render_as_string(hide_password=False),
        )
        monkeypatch.setattr(settings, "ml_backend_storage_host", None)
        model_calls, roi_pixels, legacy_urls = [], [], []
        fail_model = {"enabled": True}

        from app.application import preannotation_execution as execution
        from tests.inference_execution_test_ports import FakeModelExecutionPort

        def respond(operation, payload):
            if operation.route_owner.endswith("stage:11"):

                async def classify():
                    async with httpx.AsyncClient(trust_env=False) as http:
                        response = await http.get(payload["file_path"])
                    assert response.status_code == 200
                    with Image.open(BytesIO(response.content)) as roi:
                        roi_pixels.append(roi.getpixel((20, 20)))
                    return [{"attributes": {"color": "original-red"}}]

                return classify()
            task_id = payload["id"]
            model_calls.append(task_id)
            assert "versionId=" in payload["file_path"]

            async def detect():
                async with httpx.AsyncClient(trust_env=False) as http:
                    response = await http.get(payload["file_path"])
                assert response.status_code == 200
                assert (
                    sha256(response.content).hexdigest() == sha256(original).hexdigest()
                )
                if task_id == str(tasks[1].id) and fail_model["enabled"]:
                    raise RuntimeError("injected bulk model failure")
                return [_bbox(0, 0, 100, 100)]

            return detect()

        def mutable_url(_task):
            legacy_urls.append(_task.id)
            pytest.fail("manifest execution must not resolve a current Task URL")

        def mutable_image(_task):
            pytest.fail("manifest ROI must use the private original Input")

        monkeypatch.setattr("app.services.storage.resolve_task_url", mutable_url)
        monkeypatch.setattr(
            "app.services.prediction_context.load_task_image", mutable_image
        )
        monkeypatch.setattr(
            execution,
            "_model_execution_port",
            lambda *_args, **_kwargs: FakeModelExecutionPort(respond),
        )
        request = BatchPredictionRequest(
            project_id=str(project.id),
            ml_backend_id=str(backend.id),
            user_id=str(actor_id),
            batch_id=str(batch.id),
            execution_scope="bulk",
            async_job_id=str(header.job_id),
            predict_mode="overwrite",
            task="detect",
            pipeline_stages=[
                {
                    "stage": 3,
                    "parent_stage": None,
                    "ml_backend_id": str(backend.id),
                    "task": "detect",
                },
                {
                    "stage": 11,
                    "parent_stage": 3,
                    "ml_backend_id": str(backend.id),
                    "roi": {"pad": 0},
                    "write": {"target": "attributes", "keys": ["color"]},
                },
            ],
            accepted_input_manifest_protocol=c.MANIFEST_SCHEMA,
            accepted_input_manifest_id=str(header.job_id),
            accepted_input_manifest_digest=header.selection_digest,
        )
        # The missing source must become a fixed member failure. A late Task is
        # outside admission even though it belongs to the same still-active batch.
        await asyncio.to_thread(
            runtime.owner_s3.delete_object,
            Bucket=runtime.datasets_bucket,
            Key=source_keys[2],
        )
        async with runtime.owner_sessions.begin() as owner:
            late = await create_task(owner, project_id=project.id)
            late.batch_id = batch.id
            runtime.task_ids.append(late.id)
        with pytest.raises(PreannotationInputDeferred):
            await run_preannotation(request, execution_id=str(header.job_id))
        assert not model_calls
        async with runtime.owner_sessions() as owner:
            assert (await owner.get(AsyncJob, header.job_id)).status == "pending"
            assert (
                len(
                    (
                        await owner.scalars(
                            select(Prediction).where(
                                Prediction.project_id == project.id
                            )
                        )
                    ).all()
                )
                == 3
            )

        async with ManifestClient(origin, runtime.control_token) as manifests:
            page = await manifests.page(
                c.ManifestPageRequest(
                    **scope.model_dump(), selection_digest=header.selection_digest
                )
            )
            assert len(page.members) == 3
            assert late.id not in {member.task_id for member in page.members}
            for member in page.members:
                if member.input is None:
                    assert member.task_id == tasks[2].id and member.state == "failed"
                    continue
                process, log = runtime.start_worker(
                    f"bulk-input-{member.ordinal}", input_id=member.input.input_id
                )
                assert (
                    await _wait_process_exit(
                        process, timeout=45, message="bulk input copy did not finish"
                    )
                    == 0
                ), _process_log(log)
            sealed = await manifests.seal(
                c.SealManifestRequest(
                    **scope.model_dump(), selection_digest=header.selection_digest
                )
            )
            assert sealed.ready_count == 2 and sealed.failed_count == 1
            await _node_peer(runtime, sealed)

        await run_preannotation(request, execution_id=str(header.job_id))
        async with runtime.owner_sessions() as owner:
            job = await owner.get(AsyncJob, header.job_id)
            assert job.status == "completed", job.error_message
            assert job.result["success_count"] == 1 and job.result["failed_count"] == 2
            assert len(model_calls) == 2 and str(tasks[2].id) not in model_calls
            assert roi_pixels and all(
                abs(actual - expected) <= 2
                for actual, expected in zip(roi_pixels[0], (240, 20, 30), strict=True)
            )
            kept = (
                await owner.scalars(
                    select(Prediction).where(Prediction.task_id == tasks[2].id)
                )
            ).all()
            assert len(kept) == 1 and kept[0].score == 0.5
            written = (
                await owner.scalars(
                    select(Prediction).where(Prediction.task_id == tasks[0].id)
                )
            ).one()
            assert written.result[0]["attributes"]["color"] == "original-red"
            failures = (
                await owner.scalars(
                    select(FailedPrediction).where(
                        FailedPrediction.project_id == project.id
                    )
                )
            ).all()
            assert len(failures) == 2
            failed = {item.task_id: item for item in failures}
            for value in failures:
                assert value.extra["accepted_input_manifest"]["manifest_id"] == str(
                    header.job_id
                )
            retained = failed[tasks[1].id].extra["accepted_input"]
            assert retained["sha256"] == sha256(original).hexdigest()
            assert "accepted_input" not in failed[tasks[2].id].extra
            retained_failure, missing_failure = (
                failed[tasks[1].id].id,
                failed[tasks[2].id].id,
            )

        # Retrying a retained per-item failure survives ordinary Task versions
        # and origin Job pruning; it still cannot borrow a current source URL.
        async with runtime.owner_sessions.begin() as owner:
            await owner.execute(
                text("DELETE FROM public.async_jobs WHERE id=:job"),
                {"job": header.job_id},
            )
            await owner.execute(
                text("UPDATE public.tasks SET version=version+1 WHERE id=:task"),
                {"task": tasks[1].id},
            )
        fail_model["enabled"] = False
        retried = await _do_retry_with_factory(
            runtime.owner_sessions, str(retained_failure), str(actor_id)
        )
        assert retried["status"] == "succeeded", retried
        async with runtime.owner_sessions() as owner:
            assert await owner.get(FailedPrediction, retained_failure) is None
            prediction = (
                await owner.scalars(
                    select(Prediction).where(Prediction.task_id == tasks[1].id)
                )
            ).one()
            metadata = (
                await owner.scalars(
                    select(PredictionMeta).where(
                        PredictionMeta.prediction_id == prediction.id
                    )
                )
            ).one()
            assert metadata.extra["accepted_input"]["input_id"] == retained["input_id"]
        not_retryable = await _do_retry_with_factory(
            runtime.owner_sessions, str(missing_failure), str(actor_id)
        )
        assert not_retryable["status"] == "failed"
        assert len(model_calls) == 3 and not legacy_urls
        runtime.source_objects.append(
            (
                runtime.resources["buckets"]["MINIO_IMPORT_BUCKET"],
                f"roi-crops/{header.job_id}/{tasks[0].id}/0.jpg",
            )
        )


async def test_node_reads_1503_frozen_members_over_bounded_real_http_pages(
    test_engine, tmp_path, caplog
):
    caplog.set_level(logging.WARNING, logger="httpx")
    async with _manifest_runtime(test_engine, tmp_path) as runtime:
        async with runtime.owner_sessions.begin() as owner:
            actor_id = await _seed_actor(owner, role="super_admin")
            project = await create_project(owner, owner_id=actor_id)
            prefix = uuid4().hex[:12]
            tasks = [
                Task(
                    project_id=project.id,
                    display_id=f"T-{prefix}-{index}",
                    file_name=f"missing-{index}.png",
                    file_path="",
                    file_type="image",
                    status="pending",
                )
                for index in range(1503)
            ]
            owner.add_all(tasks)
            await owner.flush()
        runtime.track_actor(actor_id)
        runtime.project_ids.append(project.id)
        runtime.task_ids.extend(task.id for task in tasks)
        header = await _capture(runtime, actor_id, project.id)
        assert header.candidate_count == header.selected_count == 1503
        origin = str(runtime.control_client.base_url).rstrip("/")
        async with ManifestClient(origin, runtime.control_token) as manifests:
            sealed = await manifests.seal(
                c.SealManifestRequest(
                    job_id=header.job_id,
                    actor_id=actor_id,
                    project_id=project.id,
                    selection_digest=header.selection_digest,
                )
            )
            assert sealed.ready_count == 0 and sealed.failed_count == 1503
            await _node_peer(runtime, sealed)
