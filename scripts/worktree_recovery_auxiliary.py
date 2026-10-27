"""Synthetic retained-output evidence for the isolated offline recovery drill.

These fixtures exercise physical relocation and immutable facts only. They are
not native preview/inference execution records or framework qualification proof.
The caller owns random disposable resources and commits the surrounding fixture.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
import hashlib
import io
import json
import uuid

FIXTURE = "isolated-recovery-auxiliary"
MAX_BYTES = 1024 * 1024


def _bytes(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _png_facts(raw):
    from PIL import Image

    assert isinstance(raw, bytes) and 0 < len(raw) <= MAX_BYTES
    with Image.open(io.BytesIO(raw)) as image:
        assert image.format == "PNG" and 0 < image.width * image.height <= 1024**2
        image.load()
        width, height = image.size
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "width": width,
        "height": height,
    }


def _artifact(key, raw):
    return {
        "key": key,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _write(storage, owner, key, raw, metadata, content_type):
    owner.contains(key)
    assert 0 < len(raw) <= MAX_BYTES
    storage.client.put_object(
        Bucket=owner.bucket,
        Key=key,
        Body=raw,
        Metadata=metadata,
        ContentType=content_type,
    )
    return _artifact(key, raw)


def _read(storage, owner, artifact, metadata):
    owner.contains(artifact["key"])
    assert (
        type(artifact["size_bytes"]) is int and 0 < artifact["size_bytes"] <= MAX_BYTES
    )
    params = {"Bucket": owner.bucket, "Key": artifact["key"]}
    head = storage.client.head_object(**params)
    assert head["ContentLength"] == artifact["size_bytes"]
    assert head.get("Metadata") == metadata and head.get("ETag")
    response = storage.client.get_object(**params, IfMatch=head["ETag"])
    try:
        raw = response["Body"].read(artifact["size_bytes"] + 1)
    finally:
        response["Body"].close()
    assert len(raw) == artifact["size_bytes"]
    assert hashlib.sha256(raw).hexdigest() == artifact["sha256"]
    fields = ["ETag", "ContentLength", "Metadata"]
    assert all(response.get(key) == head.get(key) for key in fields)
    after = storage.client.head_object(**params)
    assert all(after.get(key) == head.get(key) for key in fields)
    return raw


def _preview_report(preview):
    return {
        "fixture": FIXTURE,
        "kind": "preview-retained-output",
        "input_digest": preview.input_digest,
        "evidence": {"nested": [{"payload": preview.spec["payload"]}]},
    }


def _evaluation_report(run, attempt):
    return {
        "fixture": FIXTURE,
        "kind": "sample-evaluation-retained-output",
        "evaluation_id": str(run.id),
        "attempt_id": str(attempt.id),
        "spec_digest": run.spec_digest,
        "evidence": {"nested": [{"payload_digest": _digest(run.spec["payload"])}]},
    }


def _authorization(context):
    return {"fixture": FIXTURE, "evidence": {"nested": [deepcopy(context)]}}


async def seed_auxiliary(
    db, *, project, actor, dataset, run, attempt, png, now, storage
):
    """Flush three explicit owners and four objects; do not commit or dispatch."""
    from sqlalchemy import select
    from app.db.models.active_learning import (
        ActiveLearningAttempt,
        ActiveLearningCandidate,
        ActiveLearningRound,
    )
    from app.db.models.sample_evaluation import (
        SampleEvaluationAttempt,
        SampleEvaluationRun,
    )
    from app.db.models.task import Task
    from app.db.models.trained_model import TrainedModelVersion
    from app.db.models.training_preview import TrainingPreview
    from app.services import active_learning_storage as active
    from app.services import sample_evaluation_storage as evaluations
    from app.services import training_preview_storage as previews
    from app.services import trained_models
    from tests.test_trained_models import ready_deployment

    payload = _png_facts(png)
    context = {
        "fixture": FIXTURE,
        "dataset_id": str(dataset.id),
        "run_id": str(run.id),
        "attempt_id": str(attempt.id),
    }
    assert (
        attempt.run_id == run.id and run.project_id == dataset.project_id == project.id
    )
    artifact = deepcopy(attempt.artifacts["best.pt"])
    assert artifact["role"] == "weights"
    model = TrainedModelVersion(
        project_id=project.id,
        creator_id=actor.id,
        run_id=run.id,
        attempt_id=attempt.id,
        request_id=uuid.uuid4(),
        request_digest=_digest({**context, "kind": "model"}),
        name="Synthetic recovery model; never executed",
        framework="synthetic-recovery",
        framework_version="fixture",
        artifact=artifact,
        classes=["car"],
        bindings=["car"],
        binding_digest=_digest({"classes": ["car"], "bindings": ["car"]}),
        provenance={**context, "execution_qualified": False, "task": "detect"},
        evaluation={"fixture": FIXTURE},
    )
    db.add(model)
    await db.flush()
    await trained_models.owner_for_model(db, model)
    _, deployment = await ready_deployment(db, project, model)

    preview_spec = {**context, "payload": payload, "count": 1}
    input_digest = _digest(preview_spec)
    request = {"fixture": FIXTURE, "input_digest": input_digest}
    preview = TrainingPreview(
        project_id=project.id,
        dataset_id=dataset.id,
        creator_id=actor.id,
        request_id=uuid.uuid4(),
        request_digest=_digest(request),
        request=request,
        input_digest=input_digest,
        spec=preview_spec,
        authorization=_authorization(context),
        status="failed",
        resource_id="recovery-fixture-cpu",
        completed_at=now,
        put_expires_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=1),
        finish_digest=_digest({"fixture": FIXTURE, "status": "failed"}),
    )
    db.add(preview)
    await db.flush()
    await previews.allocate(db, preview)
    owner = previews.resolve(preview)
    objects = {"preview-0.png": png, "report.json": _bytes(_preview_report(preview))}
    published = {}
    for name, raw in objects.items():
        key = previews.expect_artifact(owner, name, owner.key(name))
        receipt = _write(
            storage,
            owner,
            key,
            raw,
            {
                "preview-id": str(preview.id),
                "input-digest": input_digest,
                "sha256": hashlib.sha256(raw).hexdigest(),
            },
            "image/png" if name.endswith(".png") else "application/json",
        )
        published[name] = {"name": name, **receipt}
    preview.artifacts = published
    preview.upload_inventory = {
        name: {key: value for key, value in receipt.items() if key != "key"}
        for name, receipt in published.items()
    }

    evaluation_spec = {
        **context,
        "payload": payload,
        "benchmark_digest": _digest(
            {"fixture": FIXTURE, "dataset_snapshot_digest": dataset.snapshot_digest}
        ),
        "model_ref": {"model_version_id": str(model.id), "sha256": artifact["sha256"]},
    }
    evaluation = SampleEvaluationRun(
        project_id=project.id,
        creator_id=actor.id,
        dataset_id=dataset.id,
        model_version_id=model.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest=_digest(
            {"fixture": FIXTURE, "kind": "evaluation", "spec": evaluation_spec}
        ),
        name="Synthetic recovery evaluation; never executed",
        status="failed",
        spec=evaluation_spec,
        spec_digest=_digest(evaluation_spec),
        authorization=_authorization(context),
        generation=1,
        completed_at=now,
    )
    db.add(evaluation)
    await db.flush()
    await evaluations.allocate(db, evaluation)
    evaluation_attempt = SampleEvaluationAttempt(
        run_id=evaluation.id, ordinal=1, generation=1, status="failed", completed_at=now
    )
    db.add(evaluation_attempt)
    await db.flush()
    evaluation.current_attempt_id = evaluation_attempt.id
    owner = evaluations.resolve(evaluation)
    raw = _bytes(_evaluation_report(evaluation, evaluation_attempt))
    sha = hashlib.sha256(raw).hexdigest()
    key = evaluations.expect_report(
        owner,
        evaluation_attempt.id,
        owner.key(f"{evaluation_attempt.id}/{sha}.json"),
        sha,
    )
    evaluation_attempt.report = _write(
        storage,
        owner,
        key,
        raw,
        {
            "attempt-id": str(evaluation_attempt.id),
            "evaluation-id": str(evaluation.id),
            "spec-digest": evaluation.spec_digest,
            "benchmark-digest": evaluation.spec["benchmark_digest"],
            "weight-sha256": artifact["sha256"],
        },
        "application/json",
    )

    task = await db.scalar(
        select(Task).where(Task.project_id == project.id).order_by(Task.id).limit(1)
    )
    assert task is not None
    sample = {
        "ordinal": 0,
        "task_id": str(task.id),
        "dataset_item_id": str(task.dataset_item_id) if task.dataset_item_id else None,
        **payload,
        "derived_sha256": payload["sha256"],
        "preprocessing": {"fixture": FIXTURE, "identity": "already-png"},
    }
    round_spec = {**context, "payload": payload, "samples": [sample]}
    round = ActiveLearningRound(
        project_id=project.id,
        creator_id=actor.id,
        model_version_id=model.id,
        deployment_id=deployment.id,
        request_id=uuid.uuid4(),
        request_digest=_digest(
            {"fixture": FIXTURE, "kind": "active-learning", "spec": round_spec}
        ),
        name="Synthetic recovery round; never executed",
        status="failed",
        spec=round_spec,
        spec_digest=_digest(round_spec),
        authorization=_authorization(context),
        generation=1,
        completed_at=now,
    )
    db.add(round)
    await db.flush()
    await active.allocate(db, round)
    round_attempt = ActiveLearningAttempt(
        round_id=round.id,
        ordinal=1,
        generation=1,
        status="failed",
        completed_at=now,
        report={
            "fixture": FIXTURE,
            "kind": "database-only-report",
            "evidence": {"nested": [payload]},
        },
    )
    db.add(round_attempt)
    await db.flush()
    round.current_attempt_id = round_attempt.id
    owner = active.resolve(round)
    key = active.expect_media(owner, 0, owner.key("media/0.png"))
    _write(
        storage,
        owner,
        key,
        png,
        {
            "round-id": str(round.id),
            "ordinal": "0",
            "source-sha256": sample["sha256"],
            "sha256": sample["derived_sha256"],
        },
        "image/png",
    )
    db.add(
        ActiveLearningCandidate(
            round_id=round.id,
            ordinal=0,
            task_id=task.id,
            dataset_item_id=task.dataset_item_id,
            request_id=uuid.uuid4(),
            status="failed",
            media={"key": key, "sha256": sample["derived_sha256"]},
            evidence={
                "fixture": FIXTURE,
                "nested": [{"payload_digest": _digest(payload)}],
            },
            produced_attempt_id=round_attempt.id,
        )
    )
    await db.flush()
    return {"synthetic_auxiliary_owners": 3, "synthetic_auxiliary_objects": 4}


async def verify_auxiliary(db, *, storage):
    """Recheck remapped owners, complete bytes and unchanged synthetic evidence."""
    from sqlalchemy import select
    from app.db.models.active_learning import (
        ActiveLearningAttempt,
        ActiveLearningCandidate,
        ActiveLearningRound,
    )
    from app.db.models.sample_evaluation import (
        SampleEvaluationAttempt,
        SampleEvaluationRun,
    )
    from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
    from app.db.models.training_preview import TrainingPreview
    from app.services import active_learning_storage as active
    from app.services import sample_evaluation_storage as evaluations
    from app.services import training_preview_storage as previews
    from app.services import trained_models

    model = (await db.scalars(select(TrainedModelVersion))).one()
    await trained_models.owner_for_model(db, model)
    assert (
        model.provenance["fixture"] == FIXTURE
        and model.provenance["execution_qualified"] is False
    )
    deployment = (await db.scalars(select(ModelDeployment))).one()
    assert deployment.model_version_id == model.id
    assert deployment.receipt == trained_models.expected_receipt(model)
    context = {
        key: model.provenance[key]
        for key in ["fixture", "dataset_id", "run_id", "attempt_id"]
    }
    preview = (await db.scalars(select(TrainingPreview))).one()
    evaluation = (await db.scalars(select(SampleEvaluationRun))).one()
    round = (await db.scalars(select(ActiveLearningRound))).one()
    for row in [preview, evaluation, round]:
        assert row.status == "failed" and row.project_id == model.project_id
        assert row.authorization == _authorization(context)
        assert all(row.spec[key] == value for key, value in context.items())
    assert (
        str(preview.dataset_id) == str(evaluation.dataset_id) == context["dataset_id"]
    )
    assert evaluation.model_version_id == round.model_version_id == model.id
    assert evaluation.deployment_id == round.deployment_id == deployment.id

    owner = previews.resolve(preview)
    assert owner.bucket == storage.training_bucket
    assert preview.input_digest == _digest(preview.spec)
    assert preview.request == {"fixture": FIXTURE, "input_digest": preview.input_digest}
    assert preview.request_digest == _digest(preview.request)
    assert preview.finish_digest == _digest({"fixture": FIXTURE, "status": "failed"})
    assert (
        set(preview.artifacts)
        == set(preview.upload_inventory)
        == {"report.json", "preview-0.png"}
    )
    for name, receipt in preview.artifacts.items():
        previews.expect_artifact(owner, name, receipt["key"])
        assert {
            key: value for key, value in receipt.items() if key != "key"
        } == preview.upload_inventory[name]
        raw = _read(
            storage,
            owner,
            receipt,
            {
                "preview-id": str(preview.id),
                "input-digest": preview.input_digest,
                "sha256": receipt["sha256"],
            },
        )
        if name.endswith(".png"):
            assert _png_facts(raw) == preview.spec["payload"]
        else:
            assert json.loads(raw) == _preview_report(preview)

    owner = evaluations.resolve(evaluation)
    assert owner.bucket == storage.training_bucket
    assert evaluation.spec_digest == _digest(evaluation.spec)
    assert evaluation.request_digest == _digest(
        {"fixture": FIXTURE, "kind": "evaluation", "spec": evaluation.spec}
    )
    assert evaluation.spec["model_ref"] == {
        "model_version_id": str(model.id),
        "sha256": model.artifact["sha256"],
    }
    evaluation_attempt = (await db.scalars(select(SampleEvaluationAttempt))).one()
    assert (
        evaluation_attempt.id == evaluation.current_attempt_id
        and evaluation_attempt.run_id == evaluation.id
    )
    assert (
        evaluation_attempt.status == "failed"
        and evaluation_attempt.generation == evaluation.generation == 1
    )
    receipt = evaluation_attempt.report
    evaluations.expect_report(
        owner, evaluation_attempt.id, receipt["key"], receipt["sha256"]
    )
    raw = _read(
        storage,
        owner,
        receipt,
        {
            "attempt-id": str(evaluation_attempt.id),
            "evaluation-id": str(evaluation.id),
            "spec-digest": evaluation.spec_digest,
            "benchmark-digest": evaluation.spec["benchmark_digest"],
            "weight-sha256": model.artifact["sha256"],
        },
    )
    assert json.loads(raw) == _evaluation_report(evaluation, evaluation_attempt)
    assert evaluation.spec["payload"] == preview.spec["payload"]

    owner = active.resolve(round)
    assert owner.bucket == storage.training_bucket
    assert round.spec_digest == _digest(round.spec)
    assert round.request_digest == _digest(
        {"fixture": FIXTURE, "kind": "active-learning", "spec": round.spec}
    )
    assert round.spec["payload"] == preview.spec["payload"]
    round_attempt = (await db.scalars(select(ActiveLearningAttempt))).one()
    assert (
        round_attempt.id == round.current_attempt_id
        and round_attempt.round_id == round.id
    )
    assert (
        round_attempt.status == "failed"
        and round_attempt.generation == round.generation == 1
    )
    assert round_attempt.report == {
        "fixture": FIXTURE,
        "kind": "database-only-report",
        "evidence": {"nested": [round.spec["payload"]]},
    }
    candidate = (await db.scalars(select(ActiveLearningCandidate))).one()
    (sample,) = round.spec["samples"]
    assert (
        candidate.round_id == round.id
        and candidate.produced_attempt_id == round_attempt.id
    )
    assert (
        candidate.ordinal == sample["ordinal"] == 0
        and str(candidate.task_id) == sample["task_id"]
    )
    assert (
        str(candidate.dataset_item_id) if candidate.dataset_item_id else None
    ) == sample["dataset_item_id"]
    assert candidate.evidence == {
        "fixture": FIXTURE,
        "nested": [{"payload_digest": _digest(round.spec["payload"])}],
    }
    key = active.expect_media(owner, sample["ordinal"], candidate.media["key"])
    assert candidate.media["sha256"] == sample["derived_sha256"] == sample["sha256"]
    receipt = {
        "key": key,
        "sha256": sample["derived_sha256"],
        "size_bytes": round.spec["payload"]["size_bytes"],
    }
    raw = _read(
        storage,
        owner,
        receipt,
        {
            "round-id": str(round.id),
            "ordinal": "0",
            "source-sha256": sample["sha256"],
            "sha256": sample["derived_sha256"],
        },
    )
    assert _png_facts(raw) == round.spec["payload"]
    return {
        "verified_preview_owners": 1,
        "verified_preview_objects": 2,
        "verified_sample_evaluation_owners": 1,
        "verified_sample_evaluation_reports": 1,
        "verified_active_learning_owners": 1,
        "verified_active_learning_images": 1,
        "verified_auxiliary_model_source": 1,
    }
