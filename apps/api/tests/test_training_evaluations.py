"""Report evidence, pure-parse cache, independent attempts and project authority."""

import copy
import asyncio
import hashlib
import io
import json
import uuid
from types import SimpleNamespace
import threading

import pytest

from app.db.models.training_run import TrainingAttempt, TrainingRun
from app.services import training_artifacts as artifacts
from app.services import training_run_storage
from app.services.training_evaluations import SEMANTICS, parse_evaluation
from tests.factory import create_membership
from tests.test_training_runs import training_config, ready_dataset, create_run  # noqa: F401, F811


def evidence():
    run = SimpleNamespace(
        id=uuid.uuid4(),
        config_digest="c" * 64,
        config={
            "framework": "ultralytics",
            "framework_version": "8.4.177",
            "validation_digest": "d" * 64,
            "classes": ["car", "person"],
            "parameters": {"epochs": 3, "imgsz": 128},
        },
    )
    attempt = SimpleNamespace(id=uuid.uuid4(), runtime={"image_digest": "owned"})
    metrics, weights = {"sha256": "a" * 64}, {"sha256": "b" * 64}
    report = {
        "summary": {"metrics/mAP50-95(B)": 0.0, "metrics/recall(B)": -1},
        "per_class": [{"index": 1, "name": "person", "mAP50": 0.0}],
        "validation_digest": "d" * 64,
        "evaluation": {
            "framework": "ultralytics",
            "version": "8.4.177",
            "task": "detection",
        },
        "comparison": {
            "revision": 1,
            "config_digest": run.config_digest,
            "candidate": {
                "weights_sha256": weights["sha256"],
                "selection": "native_best",
                "best_epoch": None,
            },
            "benchmark": {
                "validation_digest": "d" * 64,
                "classes": ["car", "person"],
                "class_space": "frozen_native",
            },
            "evaluator": {
                "framework": "ultralytics",
                "version": "8.4.177",
                "task": "detection",
                "implementation": "owned.validator",
                "source_sha256": "e" * 64,
                "options": {
                    "split": "val",
                    "imgsz": 128,
                    "batch": 2,
                    "conf": 0.001,
                    "iou": 0.7,
                    "max_det": 300,
                    "half": False,
                    "augment": False,
                    "device": "cuda:0",
                    "workers": 2,
                    "rect": True,
                    "single_cls": False,
                    "classes": None,
                    "agnostic_nms": False,
                },
            },
            "semantics": {"metrics/mAP50-95": SEMANTICS, "metrics/recall": SEMANTICS},
        },
    }
    return report, run, attempt, metrics, weights


def test_segmentation_evaluation_keeps_box_mask_identity_and_rejects_detection_or_mask_drift():
    from training_contract.dataset import SEGMENTATION

    report, run, attempt, metrics, weights = evidence()
    run.config["configuration"] = {"identity": {"task": SEGMENTATION}}
    report["evaluation"]["task"] = "instance-segmentation"
    report["summary"] = {
        "metrics/mAP50-95(B)": 0.8,
        "metrics/mAP50-95(M)": 0.6,
        "fitness": 1.4,
    }
    report["per_class"] = [
        {"index": 1, "name": "person", "mAP50-95(B)": 0.8, "mAP50-95(M)": 0.6}
    ]
    d = report["comparison"]
    d["revision"] = 2
    d["evaluator"]["task"] = "instance-segmentation"
    d["evaluator"]["options"].update(
        overlap_mask=False,
        mask_ratio=1,
        mask_processing="ultralytics.utils.ops.process_mask",
    )
    d["semantics"] = {
        key: SEMANTICS for key in ("metrics/mAP50-95(B)", "metrics/mAP50-95(M)")
    }
    result = parse_evaluation(report, run, attempt, metrics, weights, 0, 50)
    assert result["identity_status"] == "verified"
    assert result["summary"] == {"metrics/mAP50-95(B)": 0.8, "metrics/mAP50-95(M)": 0.6}
    assert result["per_class"][0]["metrics"] == {"mAP50-95(B)": 0.8, "mAP50-95(M)": 0.6}
    for mutation in ("task", "mask_ratio", "fraction"):
        changed = copy.deepcopy(report)
        if mutation == "task":
            changed["comparison"]["evaluator"]["task"] = "detection"
        elif mutation == "mask_ratio":
            changed["comparison"]["evaluator"]["options"]["mask_ratio"] = 4
        else:
            changed["summary"]["metrics/mAP50-95(M)"] = 1.4
        with pytest.raises(ValueError):
            parse_evaluation(changed, run, attempt, metrics, weights, 0, 50)


def test_candidate_sha_and_provenance_do_not_prevent_compatible_comparison():
    report, run, attempt, metrics, weights = evidence()
    first = parse_evaluation(report, run, attempt, metrics, weights, 0, 1)
    assert first["summary"] == {"metrics/mAP50-95": 0, "metrics/recall": None}
    assert first["per_class"][0]["index"] == 1
    assert first["descriptor"].candidate.best_epoch is None
    weights = {"sha256": "f" * 64}
    report["comparison"]["candidate"]["weights_sha256"] = weights["sha256"]
    run.id, attempt.id = uuid.uuid4(), uuid.uuid4()
    second = parse_evaluation(report, run, attempt, metrics, weights, 1, 1)
    assert second["compatibility_digest"] == first["compatibility_digest"]
    assert second["per_class"] == []
    report["comparison"]["evaluator"]["options"]["conf"] = 0.002
    assert (
        parse_evaluation(report, run, attempt, metrics, weights, 0, 50)[
            "compatibility_digest"
        ]
        != first["compatibility_digest"]
    )


def test_legacy_is_displayable_without_inferred_evidence():
    report, *args = evidence()
    del report["comparison"]
    result = parse_evaluation(report, *args, 0, 50)
    assert (
        result["identity_status"] == "unknown"
        and result["compatibility_digest"] is None
    )
    assert result["reasons"] and result["summary"]["metrics/mAP50-95"] == 0
    report["comparison_unavailable_reason"] = "evaluator_not_recorded"
    unqualified = parse_evaluation(report, *args, 0, 50)
    assert unqualified["compatibility_digest"] is None
    assert unqualified["summary"]["metrics/mAP50-95"] == 0
    assert "backend" in unqualified["reasons"][0]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r.update(validation_digest="f" * 64),
        lambda r: r["comparison"]["candidate"].update(weights_sha256="f" * 64),
        lambda r: r["comparison"]["benchmark"].update(classes=["person", "car"]),
        lambda r: r["comparison"]["evaluator"].update(options={"split": "val"}),
        lambda r: r["summary"].update({"metrics/mAP50": float("nan")}),
        lambda r: r["summary"].update({"metrics/mAP50": 1.1}),
        lambda r: r["per_class"].append(copy.deepcopy(r["per_class"][0])),
        lambda r: r["per_class"][0].update(index=0),
    ],
)
def test_conflicts_and_invalid_metrics_fail_closed(mutation):
    report, *args = evidence()
    mutation(report)
    with pytest.raises(ValueError):
        parse_evaluation(report, *args, 0, 50)


def test_cache_rechecks_storage_and_attempt_metadata(monkeypatch):
    raw = b'{"summary":{"metrics/mAP50":0}}'
    attempt_id = uuid.uuid4()
    spec = {
        "key": "owned/report",
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    current = {"raw": raw, "attempt": str(attempt_id)}
    reads = []

    def get(**kwargs):
        reads.append(kwargs)
        return {
            "Body": io.BytesIO(current["raw"]),
            "Metadata": {"attempt-id": current["attempt"]},
        }

    monkeypatch.setattr(artifacts.storage.client, "get_object", get)
    first = artifacts.read_json_artifact(spec, attempt_id)
    first["summary"].clear()
    assert artifacts.read_json_artifact(spec, attempt_id)["summary"]
    current["attempt"] = str(uuid.uuid4())
    with pytest.raises(ValueError, match="identity"):
        artifacts.read_json_artifact(spec, attempt_id)
    current["attempt"], current["raw"] = str(attempt_id), b"x" * len(raw)
    with pytest.raises(ValueError, match="identity"):
        artifacts.read_json_artifact(spec, attempt_id)
    assert len(reads) == 4


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b"[]",
        b'{"a":' + b"[" * 18 + b"0" + b"]" * 18 + b"}",
    ],
)
def test_json_structure_and_nonfinite_are_bounded(monkeypatch, raw):
    attempt_id = uuid.uuid4()
    monkeypatch.setattr(
        artifacts.storage.client,
        "get_object",
        lambda **kw: {
            "Body": io.BytesIO(raw),
            "Metadata": {"attempt-id": str(attempt_id)},
        },
    )
    with pytest.raises(ValueError):
        artifacts.read_json_artifact(
            {
                "key": "owned",
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            attempt_id,
        )


@pytest.mark.usefixtures("training_config")
async def test_authorized_final_reader_is_independent_of_registration_and_current_attempt(
    db_session, httpx_client, super_admin, annotator, monkeypatch
):
    owner, token = super_admin
    project, dataset = await ready_dataset(db_session, owner)
    data, _ = await create_run(httpx_client, token, project, dataset)
    run = await db_session.get(TrainingRun, uuid.UUID(data["id"]))
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    report, _, _, _, _ = evidence()
    report["comparison"]["config_digest"] = run.config_digest
    report["comparison"]["benchmark"]["classes"] = report["per_class"] = []
    report["comparison"]["benchmark"]["classes"] = run.config["classes"]
    report["comparison"]["evaluator"]["options"]["imgsz"] = 640
    raw = json.dumps(report).encode()
    attempt.status = run.status = "succeeded"
    storage_owner = training_run_storage.resolve(run)
    attempt.artifacts = {
        "metrics.json": {
            "role": "metrics",
            "key": storage_owner.key(f"attempts/{attempt.id}/metrics.json"),
            "size_bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
        "best.pt": {
            "role": "weights",
            "key": storage_owner.key(f"attempts/{attempt.id}/best.pt"),
            "sha256": "b" * 64,
        },
    }
    historical = attempt.id
    second = TrainingAttempt(run_id=run.id, ordinal=2, status="queued")
    db_session.add(second)
    await db_session.flush()
    run.current_attempt_id = second.id
    await db_session.commit()
    monkeypatch.setattr(
        artifacts.storage.client,
        "get_object",
        lambda **kw: {
            "Body": io.BytesIO(raw),
            "Metadata": {"attempt-id": str(historical)},
        },
    )
    base = f"/api/v1/projects/{project.id}/training-runs/{run.id}/attempts"
    headers = {"Authorization": f"Bearer {token}"}
    response = await httpx_client.get(
        f"{base}/{historical}/evaluation", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["provenance"]["attempt_id"] == str(historical)
    assert response.json()["identity_status"] == "verified"
    assert (
        await httpx_client.get(f"{base}/{second.id}/evaluation", headers=headers)
    ).status_code == 409
    assert (
        await httpx_client.get(f"{base}/{uuid.uuid4()}/evaluation", headers=headers)
    ).status_code == 404
    await create_membership(
        db_session, project_id=project.id, user_id=annotator[0].id, role="annotator"
    )
    await db_session.commit()
    assert (
        await httpx_client.get(
            f"{base}/{historical}/evaluation",
            headers={"Authorization": f"Bearer {annotator[1]}"},
        )
    ).status_code == 403
    other, _ = await ready_dataset(db_session, owner)
    assert (
        await httpx_client.get(
            f"/api/v1/projects/{other.id}/training-runs/{run.id}/attempts/{historical}/evaluation",
            headers=headers,
        )
    ).status_code == 404


@pytest.mark.parametrize("missing", [False, True])
async def test_report_read_serializes_run_deletion(test_engine, monkeypatch, missing):
    from sqlalchemy import delete, text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from fastapi import HTTPException
    from app.api.v1.training_runs import get_evaluation, delete_run
    from app.db.models.project import Project
    from app.db.models.user import User
    from app.db.models.training_dataset import TrainingDatasetVersion
    from tests.factory import create_user

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    report, sample_run, _, _, _ = evidence()
    del report["comparison"]
    report["per_class"] = []
    raw = json.dumps(report).encode()
    async with maker() as seed:
        owner = await create_user(
            seed, "super_admin", f"report-race-{uuid.uuid4()}@test.local", "Report race"
        )
        project, dataset = await ready_dataset(seed, owner)
        run = TrainingRun(
            project_id=project.id,
            dataset_id=dataset.id,
            creator_id=owner.id,
            name="Report race",
            status="succeeded",
            config=sample_run.config,
            config_digest=sample_run.config_digest,
            request_id=uuid.uuid4(),
            request_digest="a" * 64,
        )
        seed.add(run)
        await seed.flush()
        await training_run_storage.allocate(seed, run)
        attempt = TrainingAttempt(
            run_id=run.id,
            ordinal=1,
            status="succeeded",
            artifacts={
                "metrics.json": {
                    "role": "metrics",
                    "key": "owned/report",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                },
                "best.pt": {"role": "weights", "sha256": "b" * 64},
            },
        )
        seed.add(attempt)
        await seed.flush()
        storage_owner = training_run_storage.resolve(run)
        attempt.artifacts = {
            name: {
                **artifact,
                "key": storage_owner.key(f"attempts/{attempt.id}/{name}"),
            }
            for name, artifact in attempt.artifacts.items()
        }
        run.current_attempt_id = attempt.id
        await seed.commit()
    entered, release = threading.Event(), threading.Event()

    def blocked_read(**kwargs):
        entered.set()
        assert release.wait(10)
        if missing:
            raise ValueError("Missing owned report")
        return {"Body": io.BytesIO(raw), "Metadata": {"attempt-id": str(attempt.id)}}

    monkeypatch.setattr(artifacts.storage.client, "get_object", blocked_read)
    deleted = []
    monkeypatch.setattr(
        artifacts.storage,
        "delete_prefix",
        lambda *args, **kwargs: deleted.append(args[0]),
    )
    delete_pid = None

    async def reading():
        async with maker() as db:
            try:
                return await get_evaluation(
                    run.id, attempt.id, 0, 50, project, owner, db
                )
            except HTTPException as error:
                return error

    async def deleting():
        nonlocal delete_pid
        async with maker() as db:
            delete_pid = await db.scalar(text("select pg_backend_pid()"))
            await delete_run(run.id, project, owner, db)

    read_task = asyncio.create_task(reading())
    delete_task = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        delete_task = asyncio.create_task(deleting())

        async def blocked():
            async with maker() as observer:
                while delete_pid is None or not await observer.scalar(
                    text("select cardinality(pg_blocking_pids(:pid)) > 0"),
                    {"pid": delete_pid},
                ):
                    await asyncio.sleep(0.01)

        await asyncio.wait_for(blocked(), 5)
        assert not deleted and not delete_task.done()
        release.set()
        result = await asyncio.wait_for(read_task, 5)
        await asyncio.wait_for(delete_task, 5)
        if missing:
            assert isinstance(result, HTTPException) and result.status_code == 422
        else:
            assert result["identity_status"] == "unknown"
        assert deleted == [f"projects/{project.id}/runs/{run.id}/"]
        async with maker() as verify:
            assert await verify.get(TrainingRun, run.id) is None
            assert await verify.get(TrainingAttempt, attempt.id) is None
    finally:
        release.set()
        await asyncio.gather(
            read_task, *([delete_task] if delete_task else []), return_exceptions=True
        )
        async with maker() as cleanup:
            await cleanup.execute(delete(TrainingRun).where(TrainingRun.id == run.id))
            await cleanup.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.id == dataset.id
                )
            )
            await cleanup.execute(delete(Project).where(Project.id == project.id))
            await cleanup.execute(delete(User).where(User.id == owner.id))
            await cleanup.commit()


def test_segmentation_without_comparison_keeps_canonical_task():
    from app.schemas.training_run import TrainingEvaluationOut

    report, run, attempt, metrics, weights = evidence()
    run.config["task"] = "segment"
    report.pop("comparison")
    report["evaluation"]["task"] = "instance-segmentation"
    report["summary"] = {"metrics/mAP50-95(M)": 0.4}
    report["per_class"] = []
    parsed = parse_evaluation(report, run, attempt, metrics, weights, 0, 100)
    assert TrainingEvaluationOut.model_validate(parsed).task == "segment"
