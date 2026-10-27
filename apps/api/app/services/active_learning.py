"""Durable selection rounds; predictions never become annotation truth here."""

from __future__ import annotations

import hashlib
import time
import uuid
from datetime import datetime, timezone

from botocore.exceptions import ClientError
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.active_learning import (
    ActiveLearningAttempt as Attempt,
    ActiveLearningCandidate as Candidate,
    ActiveLearningDecision as Decision,
    ActiveLearningPoolMember as Member,
    ActiveLearningReference as Reference,
    ActiveLearningRound as Round,
)
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.trained_model import ModelDeployment, TrainedModelVersion
from app.schemas.active_learning import RoundOut, Selection
from app.schemas.batch import BatchCreate
from app.schemas.sample_evaluation import DetectionPrediction, EvaluationProfile
from app.services import (
    active_learning_engine as engine,
    active_learning_pool as pool,
    active_learning_storage as owners,
    active_learning_originals as originals,
    async_job as jobs,
    managed_inference as managed,
    sample_evaluation_engine as detection,
    trained_models,
)
from app.services.async_job_notify import notify_job_terminal
from app.services.batch import BatchService
from app.services.storage import storage_service as storage
from app.services import training_storage_io as storage_io
from app.services import training_write_accounting as write_accounting
from contextlib import nullcontext
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    sample_identities,
)
from app.services.training_datasets import assert_training_authority

ACTIVE = {"queued", "running", "cancelling", "lost"}
TERMINAL = {"succeeded", "failed", "cancelled"}
reject = managed.reject


def now():
    return datetime.now(timezone.utc)


async def authority(db, round):
    if round.spec.get("original_contract") == 1:
        try:
            await originals.delegated(db, round, commit=False)
        except HTTPException as exc:
            if exc.status_code not in {401, 403}:
                raise
            code = (
                exc.detail.get("code", "authority_revoked")
                if isinstance(exc.detail, dict)
                else "authority_revoked"
            )
            raise TrainingDatasetError(
                code, "选样创建者或原密钥权限已撤销", status_code=exc.status_code
            ) from None
        return
    await assert_training_authority(
        db,
        project_id=round.project_id,
        actor_id=round.creator_id,
        api_key_id=round.authorization.get("api_key_id"),
        required_scopes=pool.SCOPES,
    )


async def load_round(db, project_id, round_id, *, lock=False):
    query = select(Round).where(Round.id == round_id, Round.project_id == project_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    round = await db.scalar(query)
    if round is None:
        raise reject("round_missing", "选样轮次不存在", 404)
    return round


async def candidate_rows(db, round):
    return list(
        (
            await db.scalars(
                select(Candidate)
                .where(Candidate.round_id == round.id)
                .order_by(Candidate.ordinal)
                .execution_options(populate_existing=True)
            )
        ).all()
    )


def validate_spec(round):
    spec = round.spec
    try:
        checked = Selection.model_validate(
            {
                **{
                    key: spec[key]
                    for key in (
                        "seed",
                        "batch_size",
                        "max_inference_dispatches",
                        "max_review_tasks",
                        "confidence",
                        "nms_iou",
                    )
                },
                "sample_task_ids": [sample["task_id"] for sample in spec["pool"]],
                "model_version_id": round.model_version_id,
                "deployment_id": round.deployment_id,
            }
        )
        ref, profile = spec["model_ref"], spec["profile"]
        segment = engine.strategy_for(ref) == engine.MASK_STRATEGY
        prepared = {
            key: value
            for key, value in spec.items()
            if key not in {"round_id", "issued_at", "expires_at", "preview_digest"}
        }
        samples, omitted = engine.grouped_pool(
            spec["pool"], spec["exclusions"]["identities"]
        )
        samples = engine.partition(samples, checked.seed, checked.batch_size)
        if (
            spec["schema"] != (2 if segment else 1)
            or (
                segment
                and (spec.get("task") != "segment" or spec.get("schema_version") != 2)
            )
            or content_digest(spec) != round.spec_digest
            or spec["round_id"] != str(round.id)
            or spec["preview_digest"] != content_digest(prepared)
            or spec["pool_digest"] != content_digest(spec["pool"])
            or samples != spec["samples"]
            or omitted != spec["omitted"]
            or spec["exclusions"]["digest"]
            != content_digest(
                {
                    key: value
                    for key, value in spec["exclusions"].items()
                    if key != "digest"
                }
            )
            or ref["project_id"] != str(round.project_id)
            or ref["model_version_id"] != str(round.model_version_id)
            or ref["deployment_id"] != str(round.deployment_id)
            or ref["reference_digest"]
            != content_digest(
                {key: value for key, value in ref.items() if key != "reference_digest"}
            )
            or spec["profile_digest"] != profile["profile_digest"]
            or profile["profile_digest"]
            != content_digest(
                {
                    key: value
                    for key, value in profile.items()
                    if key != "profile_digest"
                }
            )
            or EvaluationProfile.model_validate(profile).model_dump(
                mode="json", by_alias=True
            )
            != profile
            or spec["strategy"] != engine.strategy_for(ref)
            or spec["preprocessing"] != detection.PREPROCESSING
            or type(spec["issued_at"]) is not int
            or spec["issued_at"] <= 0
            or spec["expires_at"] != spec["issued_at"] + 86400
            or len(samples) > checked.max_inference_dispatches
            or len(ref["classes"]) != len(ref["bindings"])
            or len(set(ref["bindings"])) != len(ref["bindings"])
        ):
            raise ValueError("Frozen round differs")
        for sample in spec["pool"]:
            if "original_contract" in spec:
                if (
                    type(spec["original_contract"]) is not int
                    or spec["original_contract"] != 1
                ):
                    raise ValueError("Unknown original input contract")
                frozen = originals.validate_receipt(sample["snapshot"].get("original"))
                if (
                    frozen["sha256"] != sample["sha256"]
                    or frozen["size_bytes"] != sample["size_bytes"]
                ):
                    raise ValueError("Frozen original identity differs")
            if (
                not 0 < sample["size_bytes"] <= detection.MAX_RAW_BYTES
                or sample["width"] * sample["height"] > detection.MAX_PIXELS
                or sample["preprocessing"]["raw_sha256"] != sample["sha256"]
                or sample["preprocessing"]["derived_sha256"] != sample["derived_sha256"]
            ):
                raise ValueError("Frozen source differs")
            if segment:
                from app.services import sample_evaluation_masks as masks

                if (
                    sample["width"] * sample["height"] > masks.MAX_PIXELS
                    or max(sample["width"], sample["height"]) > masks.MAX_DIMENSION
                ):
                    raise ValueError(
                        "Frozen segmentation source exceeds its profile budget"
                    )
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise reject("spec_corrupt", "冻结选样规格未通过身份与预算检查") from exc
    return spec


async def retain_references(db, round, references):
    existing = (
        await db.scalars(select(Reference).where(Reference.round_id == round.id))
    ).all()
    ids = {
        ("dataset", str(row.dataset_version_id))
        if row.dataset_version_id
        else ("evaluation", str(row.evaluation_id))
        for row in existing
    }
    for ref in references:
        if (ref["kind"], ref["id"]) in ids:
            continue
        db.add(
            Reference(
                round_id=round.id,
                dataset_version_id=uuid.UUID(ref["id"])
                if ref["kind"] == "dataset"
                else None,
                evaluation_id=uuid.UUID(ref["id"])
                if ref["kind"] == "evaluation"
                else None,
                source_digest=ref["digest"],
            )
        )
        ids.add((ref["kind"], ref["id"]))


async def create_round(db, project, user, body, prepared, api_key_id=None):
    context = prepared["_authority"]
    digest = content_digest(body.model_dump(mode="json"))
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await assert_training_authority(
        db,
        project_id=project.id,
        actor_id=user.id,
        api_key_id=api_key_id,
        required_scopes=pool.SCOPES,
    )
    existing = await db.scalar(
        select(Round).where(
            Round.project_id == project.id, Round.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise reject("request_conflict", "同一请求标识不能用于不同选样轮次")
        return existing
    if prepared["preview_digest"] != body.preview_digest:
        raise reject("preview_changed", "候选、模型或预算已变化，请重新预览")
    for model, identity in (
        (TrainedModelVersion, body.model_version_id),
        (ModelDeployment, body.deployment_id),
    ):
        if (
            await db.scalar(
                select(model).where(model.id == identity).with_for_update(read=True)
            )
            is None
        ):
            raise reject("model_missing", "固定模型或部署已不可用")
    await trained_models.validate_frozen_reference(
        db, project.id, prepared["model_ref"]
    )
    ids = body.sample_task_ids
    await db.execute(
        select(Task.id)
        .where(Task.project_id == project.id, Task.id.in_(ids))
        .order_by(Task.id)
        .with_for_update()
    )
    item_ids = [
        uuid.UUID(sample["snapshot"]["dataset_item_id"])
        for sample in prepared["pool"]
        if sample["snapshot"]["dataset_item_id"]
    ]
    await pool.lock_source_items(db, item_ids)
    await pool.lock_unedited(db, ids, user.id)
    if (
        await pool.snapshots(db, project.id, ids)
        != [sample["snapshot"] for sample in prepared["pool"]]
        or await pool.exclusions(db, project.id, lock=True) != prepared["exclusions"]
    ):
        raise reject("pool_stale", "创建前候选资格或历史排除成员已变化，请重新预览")
    identity, issued = uuid.uuid4(), int(time.time())
    for source in prepared["_live_sources"]:
        await originals.recheck(db, context, source, commit=False)
    spec = {
        **{key: value for key, value in prepared.items() if not key.startswith("_")},
        "round_id": str(identity),
        "issued_at": issued,
        "expires_at": issued + 86400,
    }
    round = Round(
        id=identity,
        project_id=project.id,
        creator_id=user.id,
        model_version_id=body.model_version_id,
        deployment_id=body.deployment_id,
        request_id=body.request_id,
        request_digest=digest,
        name=body.name.strip(),
        spec=spec,
        spec_digest=content_digest(spec),
        authorization={
            "api_key_id": api_key_id,
            "original_contract": 1,
            "api_key_fingerprint": originals.key_fingerprint(context.key),
        },
        generation=0,
        inference_dispatches=0,
        accepted_tasks=0,
    )
    validate_spec(round)
    db.add(round)
    await db.flush()
    await owners.allocate(db, round)
    for sample in spec["pool"]:
        db.add(
            Member(
                round_id=round.id,
                task_id=uuid.UUID(sample["task_id"]),
                dataset_item_id=uuid.UUID(sample["snapshot"]["dataset_item_id"])
                if sample["snapshot"]["dataset_item_id"]
                else None,
            )
        )
    for sample in spec["samples"]:
        db.add(
            Candidate(
                round_id=round.id,
                ordinal=sample["ordinal"],
                task_id=uuid.UUID(sample["task_id"]),
                dataset_item_id=uuid.UUID(sample["snapshot"]["dataset_item_id"])
                if sample["snapshot"]["dataset_item_id"]
                else None,
                request_id=uuid.uuid5(round.id, f"candidate:{sample['ordinal']}"),
            )
        )
    await retain_references(db, round, spec["exclusions"]["references"])
    await new_attempt(db, round)
    await originals.check(db, context, commit=False)
    return round


async def new_attempt(db, round):
    owners.resolve(round)
    validate_spec(round)
    round.generation += 1
    ordinal = (
        await db.scalar(
            select(func.max(Attempt.ordinal)).where(Attempt.round_id == round.id)
        )
        or 0
    ) + 1
    attempt = Attempt(round_id=round.id, ordinal=ordinal, generation=round.generation)
    db.add(attempt)
    await db.flush()
    job = await jobs.create_job(
        db,
        kind="active_learning_round",
        user_id=round.creator_id,
        project_id=round.project_id,
        payload={
            "round_id": str(round.id),
            "attempt_id": str(attempt.id),
            "name": round.name,
        },
    )
    round.current_attempt_id, round.async_job_id = attempt.id, job.id
    attempt.async_job_id = job.id
    round.status = "queued"
    round.error_code = round.error_message = round.completed_at = None
    return attempt


def media_descriptor(round, sample):
    return {
        "key": owners.resolve(round).key(f"media/{sample['ordinal']}.png"),
        "sha256": sample["derived_sha256"],
    }


def read_retained_png(round, sample, *, transport=None, observation=None):
    transport = storage if transport is None else transport
    owner = owners.resolve(round)
    artifact = media_descriptor(round, sample)
    owners.expect_media(owner, sample["ordinal"], artifact["key"])
    params = {"Bucket": owner.bucket, "Key": artifact["key"]}
    head = transport.client.head_object(**params)
    size = head.get("ContentLength")
    if type(size) is not int or not 0 < size <= detection.MAX_PNG_BYTES:
        raise ValueError("Retained source exceeds image budget")
    response = transport.client.get_object(**params, IfMatch=head["ETag"])
    try:
        png = response["Body"].read(size + 1)
    finally:
        originals.storage_access.close_registered(response["Body"])
    if (
        len(png) != size
        or hashlib.sha256(png).hexdigest() != artifact["sha256"]
        or head.get("Metadata", {})
        != {
            "round-id": str(round.id),
            "ordinal": str(sample["ordinal"]),
            "source-sha256": sample["sha256"],
            "sha256": artifact["sha256"],
        }
    ):
        raise ValueError("Retained source identity differs")
    if observation is not None:
        observation.readback(response)
    return png


async def prepare_image(db, round, sample, *, guard=None):
    owner = owners.resolve(round)
    context = await originals.delegated(db, round)
    artifact = media_descriptor(round, sample)
    route = await storage_io.artifact_reads.resolve(
        db, owner.ref, artifact["key"], artifact["sha256"]
    )
    if route is not None:
        await originals.check(db, context, guard=guard)
        png = await storage_io.read(
            db,
            owner,
            "active_learning_media",
            lambda transport: read_retained_png(round, sample, transport=transport),
            object_key=artifact["key"],
            sha256=artifact["sha256"],
        )
        await originals.check(db, context, commit=False, guard=guard)
        if owners.resolve(round) != owner:
            raise originals.ContentError("content_binding_changed")
        await storage_io.artifact_reads.publications.recheck(db, route)
        await db.commit()
        originals.access.check_clock(context.proof, context.key)
        return png
    target = await storage_io.prepare(db, owner)
    location_id = uuid.UUID(owner.ref["location_id"])
    permit = await originals.storage_access.acquire(
        db, location_id, "active_learning_media"
    )
    accounting, failure = None, None
    try:
        async with permit:
            await originals.check(db, context, guard=guard)

            def retained_or_missing(transport):
                # Classify the exact provider result before S3Access translates it.
                # A failed client close still raises before any fallback can begin.
                try:
                    return read_retained_png(round, sample, transport=transport)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") in {
                        "404",
                        "NoSuchKey",
                        "NotFound",
                    }:
                        return None
                    raise

            png = await originals.bounded_thread(
                storage_io.physical,
                target,
                retained_or_missing,
            )
            if png is None:
                if round.spec.get("original_contract") != 1:
                    raise originals.ContentError("original_input_unrecorded") from None
                async with originals.input_image(
                    db, context, sample["snapshot"], guard=guard
                ) as (png, media, _source):
                    if any(
                        media[key] != sample[key]
                        for key in (
                            "sha256",
                            "width",
                            "height",
                            "size_bytes",
                            "derived_sha256",
                            "preprocessing",
                        )
                    ):
                        raise ValueError("Current source differs from the frozen round")
                    await originals.check(db, context, guard=guard)
                    accounting = write_accounting.recorder(
                        db,
                        owner,
                        round.id,
                        producer="active_learning_media",
                        purpose="other_platform",
                    )
                    await originals.bounded_thread(
                        storage_io.physical,
                        target,
                        lambda transport: _store_png(
                            round, sample, owner, png, transport=transport
                        ),
                        accounting=accounting,
                    )
            await originals.check(db, context, commit=False, guard=guard)
            if owners.resolve(round) != owner:
                raise originals.ContentError("content_binding_changed")
            await originals.storage_access.check(
                db, location_id, expected_revision=permit.revision, lock=True
            )
            await db.commit()
            originals.access.check_clock(context.proof, context.key)
    except BaseException as exc:
        failure = exc
    if accounting is not None:
        failure = await write_accounting.finish(
            accounting,
            failure,
            settled=getattr(permit, "_settled", False) is True,
        )
    if failure is not None:
        raise failure
    return png


def _store_png(round, sample, owner, png, *, transport=None):
    transport = storage if transport is None else transport
    artifact = media_descriptor(round, sample)
    owners.expect_media(owner, sample["ordinal"], artifact["key"])
    accounting = getattr(transport, "_write_accounting", None)
    observation = (
        write_accounting.Observation(
            accounting,
            transport.client,
            artifact["key"],
            {"sha256": artifact["sha256"], "size_bytes": len(png)},
        )
        if accounting is not None
        else None
    )
    with observation.hooks() if observation is not None else nullcontext():
        transport.client.put_object(
            Bucket=owner.bucket,
            Key=artifact["key"],
            Body=png,
            ContentType="image/png",
            Metadata={
                "round-id": str(round.id),
                "ordinal": str(sample["ordinal"]),
                "source-sha256": sample["sha256"],
                "sha256": artifact["sha256"],
            },
        )
    if (
        read_retained_png(round, sample, transport=transport, observation=observation)
        != png
    ):
        raise ValueError("Published PNG differs from the frozen input")
    if observation is not None:
        observation.complete()


def successful_evidence(round, attempt, row, result):
    spec = validate_spec(round)
    sample = spec["samples"][row.ordinal]
    if (
        row.task_id != uuid.UUID(sample["task_id"])
        or row.request_id != uuid.uuid5(round.id, f"candidate:{row.ordinal}")
        or row.request != managed.request_identity(spec, row, sample["preprocessing"])
    ):
        raise ValueError("Candidate request differs from the frozen round")
    trained_models.validate_result_runtime(result.meta, spec["model_ref"])
    inference = (result.meta or {}).get("sample_evaluation")
    managed.verify_inference(spec, row, inference)
    identity = {
        key: spec["model_ref"][key]
        for key in ("model_version_id", "sha256", "classes_digest")
    }
    if (
        str(result.task_id) != sample["task_id"]
        or (result.meta or {}).get("trained_model") != identity
    ):
        raise ValueError("Prediction task or native model differs")
    if engine.strategy_for(spec["model_ref"]) == engine.MASK_STRATEGY:
        from app.services.sample_evaluation_masks import normalize_predictions
        from app.services.controlled_segmentation import runtime_for_reference

        predictions = normalize_predictions(
            {"result": result.result, "meta": result.meta},
            list(range(len(spec["model_ref"]["classes"]))),
            width=sample["width"],
            height=sample["height"],
            runtime_qualification=runtime_for_reference(
                spec["model_ref"], spec["profile"]["ultralytics_version"]
            ),
        )
    else:
        predictions = detection.predictions_from_wire(
            result.result, list(range(len(spec["model_ref"]["classes"])))
        )
    evidence = {
        "ordinal": row.ordinal,
        "task_id": sample["task_id"],
        "sha256": sample["sha256"],
        "derived_sha256": sample["derived_sha256"],
        "request_id": str(row.request_id),
        "binding_digest": row.request["evaluation"]["binding_digest"],
        "produced_attempt_id": str(attempt.id),
        "predictions": predictions,
        "features": engine.prediction_features(predictions, spec["model_ref"]),
        "inference": inference,
    }
    return {**evidence, "evidence_digest": content_digest(evidence)}


def verified_evidence(round, row):
    spec = validate_spec(round)
    sample, evidence = spec["samples"][row.ordinal], row.evidence
    if (
        not evidence
        or row.status != "succeeded"
        or row.request != managed.request_identity(spec, row, sample["preprocessing"])
    ):
        raise reject("evidence_unavailable", "候选尚无已核验的成功证据")
    if engine.strategy_for(spec["model_ref"]) == engine.MASK_STRATEGY:
        from app.services.sample_evaluation_masks import checked_predictions

        predictions = checked_predictions(
            evidence["predictions"],
            class_count=len(spec["model_ref"]["classes"]),
            native_to_benchmark=list(range(len(spec["model_ref"]["classes"]))),
        )
        if any(
            row["rle"]["size"] != [sample["height"], sample["width"]]
            for row in predictions
        ):
            raise reject("evidence_corrupt", "掩码证据尺寸与冻结原图不同")
    else:
        predictions = [
            DetectionPrediction.model_validate(prediction).model_dump(mode="json")
            for prediction in evidence["predictions"]
        ]
    expected = {
        "ordinal": row.ordinal,
        "task_id": sample["task_id"],
        "sha256": sample["sha256"],
        "derived_sha256": sample["derived_sha256"],
        "request_id": str(row.request_id),
        "binding_digest": row.request["evaluation"]["binding_digest"],
        "produced_attempt_id": str(row.produced_attempt_id),
        "predictions": predictions,
        "features": engine.prediction_features(predictions, spec["model_ref"]),
        "inference": evidence["inference"],
    }
    if (
        len(predictions) > detection.MAX_PREDICTIONS
        or any(
            prediction["class_index"] >= len(spec["model_ref"]["classes"])
            for prediction in predictions
        )
        or evidence != {**expected, "evidence_digest": content_digest(expected)}
        or row.task_id != uuid.UUID(sample["task_id"])
    ):
        raise reject("evidence_corrupt", "候选证据或评分未通过完整性检查")
    managed.verify_inference(spec, row, evidence["inference"])
    return evidence


async def build_report(db, round, attempt):
    rows = await candidate_rows(db, round)
    if [row.ordinal for row in rows] != list(range(len(round.spec["samples"]))):
        raise reject("evidence_corrupt", "候选序号与冻结窗口不一致")
    attempts = set(
        (await db.scalars(select(Attempt.id).where(Attempt.round_id == round.id))).all()
    )
    evidence = {row.ordinal: verified_evidence(round, row) for row in rows}
    if any(row.produced_attempt_id not in attempts for row in rows):
        raise reject("evidence_corrupt", "候选证据不属于本轮次的执行尝试")
    report = {
        "schema": round.spec["schema"],
        "round_id": str(round.id),
        "attempt_id": str(attempt.id),
        "spec_digest": round.spec_digest,
        "strategy": engine.strategy_for(round.spec["model_ref"]),
        "ranking": engine.ranking(round.spec, evidence),
        "missing_signal_count": sum(
            not item["predictions"] for item in evidence.values()
        ),
        "inference_dispatches": round.inference_dispatches,
        "max_review_tasks": round.spec["max_review_tasks"],
        "claim": "workflow_correctness_only",
    }
    return {**report, "report_digest": content_digest(report)}


async def read_report(db, round, attempt):
    if (
        attempt.round_id != round.id
        or attempt.status != "succeeded"
        or not attempt.report
    ):
        raise reject("report_pending", "此执行尝试尚无完整的已核验选样结果")
    # The dispatch counter is frozen in the report; later recovery never rewrites it.
    expected = await build_report(db, round, attempt)
    if attempt.report != expected:
        raise reject("report_corrupt", "选样报告未通过完整性检查")
    return attempt.report


async def mark_terminal(
    db, round, attempt, status, *, code=None, message=None, report=None
):
    round.status = attempt.status = status
    round.completed_at = attempt.completed_at = now()
    round.error_code = attempt.error_code = code
    round.error_message = attempt.error_message = message
    attempt.report = report
    attempt.claim_id = None
    result = {"round_id": str(round.id), "attempt_id": str(attempt.id)}
    if status == "succeeded":
        await jobs.mark_complete(db, attempt.async_job_id, result=result)
    elif status == "cancelled":
        await jobs.mark_cancelled(db, attempt.async_job_id, result=result)
    else:
        await jobs.mark_failed(
            db, attempt.async_job_id, error=message or "选样未完成", result=result
        )
    await notify_job_terminal(db, job_id=attempt.async_job_id)


async def request_cancel(db, round, *, code=None, message=None):
    if round.status in TERMINAL:
        return
    attempt = await db.get(Attempt, round.current_attempt_id)
    rows = await candidate_rows(db, round)
    if round.status != "cancelling" or code is not None:
        round.error_code, round.error_message = code, message
    if not any(row.request for row in rows):
        await mark_terminal(
            db,
            round,
            attempt,
            "failed" if code else "cancelled",
            code=code,
            message=message,
        )
    else:
        round.status = attempt.status = "cancelling"
        await jobs.request_cancel(db, attempt.async_job_id)


async def signal_interrupted(db, job, *, cancelled):
    round = await db.scalar(
        select(Round).where(Round.async_job_id == job.id).with_for_update()
    )
    if not round or round.status in TERMINAL:
        return
    attempt = await db.get(Attempt, round.current_attempt_id)
    if not attempt or attempt.async_job_id != job.id:
        return
    if cancelled or round.status == "cancelling":
        await request_cancel(db, round)
    else:
        round.status = attempt.status = "lost"
        round.error_code = attempt.error_code = "execution_lost"
        round.error_message = attempt.error_message = (
            "选样执行失联；原请求可能仍在运行，恢复前须核对同一请求收据"
        )
        await jobs.mark_failed(db, job.id, error=round.error_message)
        await notify_job_terminal(db, job_id=job.id)
    attempt.claim_id = None


async def output(db, round):
    validate_spec(round)
    attempts = (
        await db.scalars(
            select(Attempt)
            .where(Attempt.round_id == round.id)
            .order_by(Attempt.ordinal.desc())
            .limit(20)
        )
    ).all()
    public_spec = {
        key: value
        for key, value in round.spec.items()
        if key not in {"pool", "exclusions", "samples"}
    }
    public_spec.update(
        pool_size=len(round.spec["pool"]),
        candidate_count=len(round.spec["samples"]),
        exclusion_reference_count=len(round.spec["exclusions"]["references"]),
        samples=[
            {key: value for key, value in sample.items() if key != "snapshot"}
            for sample in round.spec["samples"]
        ],
    )
    value = RoundOut.model_validate(round).model_dump(mode="json")
    value.update(
        spec=public_spec,
        attempts=[
            {
                "id": str(attempt.id),
                "ordinal": attempt.ordinal,
                "status": attempt.status,
                "heartbeat_at": attempt.heartbeat_at,
                "started_at": attempt.started_at,
                "completed_at": attempt.completed_at,
                "error_code": attempt.error_code,
                "error_message": attempt.error_message,
                "has_report": bool(attempt.report),
            }
            for attempt in attempts
        ],
    )
    return value


def decision_output(decision):
    if content_digest(decision.snapshot) != decision.snapshot_digest:
        raise reject("decision_corrupt", "历史选样决策未通过完整性检查")
    return {
        "id": decision.id,
        "request_id": decision.request_id,
        "action": decision.action,
        "accepted_batch_id": decision.accepted_batch_id,
        "snapshot": decision.snapshot,
        "snapshot_digest": decision.snapshot_digest,
        "created_at": decision.created_at,
    }


async def decide(db, project, user, round_id, body, api_key_id=None, *, request):
    context = await originals.authorize(db, project.id, user.id, request, api_key_id)
    digest = content_digest(body.model_dump(mode="json"))
    round = await load_round(db, project.id, round_id)
    existing = await db.scalar(
        select(Decision).where(
            Decision.round_id == round.id, Decision.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise reject("request_conflict", "同一请求标识不能用于不同选样决策")
        return existing
    attempt = await db.get(Attempt, round.current_attempt_id)
    report = await read_report(db, round, attempt)
    if round.status != "succeeded":
        raise reject("round_incomplete", "仅完整成功的选样轮次可接纳或拒绝建议")
    ranked = {item["ordinal"]: item for item in report["ranking"]}
    if any(
        ordinal not in ranked
        or body.action == "accept"
        and not ranked[ordinal]["recommended"]
        for ordinal in body.ordinals
    ):
        raise reject(
            "invalid_selection", "只能接纳本轮建议或随机对照中计划内的候选", 422
        )
    selected = [round.spec["samples"][ordinal] for ordinal in sorted(body.ordinals)]
    if body.action == "accept" and round.spec.get("original_contract") != 1:
        raise originals.ContentError("original_input_unrecorded")
    ids = [uuid.UUID(sample["task_id"]) for sample in selected]
    group_ids = sorted(
        {
            uuid.UUID(task_id)
            for sample in selected
            for task_id in sample["group_task_ids"]
        }
    )
    group_sources = sorted(
        (
            source
            for source in round.spec["pool"]
            if uuid.UUID(source["task_id"]) in group_ids
        ),
        key=lambda source: source["task_id"],
    )
    before = (
        await pool.snapshots(db, project.id, group_ids)
        if body.action == "accept"
        else None
    )
    await db.commit()
    live_sources = []
    if before:
        for sample, snapshot in zip(group_sources, before, strict=True):
            if snapshot != sample["snapshot"]:
                raise reject(
                    "source_stale", "当前任务、分派或原件身份已变化，请重新选样"
                )
            try:
                _png, media, source = await originals.read_input(db, context, snapshot)
                live_sources.append(source)
            except originals.ContentError:
                raise
            except (ValueError, OSError) as exc:
                if getattr(exc, "storage_operation_unsettled", False):
                    raise
                raise reject("source_stale", "当前来源无法核验，请重新选样") from exc
            if (
                snapshot != sample["snapshot"]
                or media["sha256"] != sample["sha256"]
                or media["derived_sha256"] != sample["derived_sha256"]
            ):
                raise reject("source_stale", "当前任务、分派或图片已变化，请重新选样")
    await db.execute(
        select(func.pg_advisory_xact_lock(func.hashtextextended(str(project.id), 0)))
    )
    await assert_training_authority(
        db,
        project_id=project.id,
        actor_id=user.id,
        api_key_id=api_key_id,
        required_scopes=pool.SCOPES,
    )
    round = await load_round(db, project.id, round_id, lock=True)
    existing = await db.scalar(
        select(Decision).where(
            Decision.round_id == round.id, Decision.request_id == body.request_id
        )
    )
    if existing:
        if existing.request_digest != digest:
            raise reject("request_conflict", "同一请求标识不能用于不同选样决策")
        return existing
    attempt = await db.get(Attempt, round.current_attempt_id, populate_existing=True)
    current_report = await read_report(db, round, attempt)
    if round.status != "succeeded" or report != current_report:
        raise reject("round_stale", "选样结果已变化，请重新检查")
    rows = await candidate_rows(db, round)
    if any(rows[ordinal].disposition for ordinal in body.ordinals):
        raise reject("candidate_decided", "所选候选已有决策，请刷新结果")
    batch, history = None, None
    if body.action == "accept":
        if round.accepted_tasks + len(ids) > round.spec["max_review_tasks"]:
            raise reject("review_budget", "接纳任务超过本轮显式审核预算")
        batch_ids = sorted(
            {
                uuid.UUID(snapshot["batch"]["id"])
                for snapshot in before
                if snapshot["batch"]
            }
        )
        if batch_ids:
            await db.execute(
                select(TaskBatch.id)
                .where(TaskBatch.id.in_(batch_ids))
                .order_by(TaskBatch.id)
                .with_for_update()
            )
        await db.execute(
            select(Task.id)
            .where(Task.id.in_(group_ids), Task.project_id == project.id)
            .order_by(Task.id)
            .with_for_update()
        )
        item_ids = sorted(
            {
                uuid.UUID(snapshot["dataset_item_id"])
                for snapshot in before
                if snapshot["dataset_item_id"]
            }
        )
        await pool.lock_source_items(db, item_ids)
        await pool.lock_unedited(db, group_ids, user.id)
        if before != await pool.snapshots(db, project.id, group_ids):
            raise reject("source_stale", "接纳前任务资格、分派或编辑锁已变化")
        for source in live_sources:
            await originals.recheck(db, context, source, commit=False)
        history = await pool.exclusions(db, project.id, lock=True)
        excluded = set(history["identities"])
        for sample in selected:
            members = [
                member
                for member in round.spec["pool"]
                if member["task_id"] in sample["group_task_ids"]
            ]
            if any(
                excluded.intersection(sample_identities(member)) for member in members
            ):
                raise reject(
                    "source_stale", "所选组已进入保留的训练、验证或测试成员，请重新选样"
                )
        batches = BatchService(db)
        batch = await batches.create(
            project.id,
            BatchCreate(
                name=body.batch_name,
                description=f"主动学习轮次 {round.name}；须按普通流程分派、标注与审核",
            ),
            user.id,
        )
        await batches.assign_tasks_to_batch(batch.id, ids)
        for old_id in batch_ids:
            await batches.recalculate_counters(old_id)
        round.accepted_tasks += len(ids)
        await retain_references(db, round, history["references"])
    snapshot = {
        "round_id": str(round.id),
        "spec_digest": round.spec_digest,
        "attempt_id": str(attempt.id),
        "report_digest": report["report_digest"],
        "ordinals": sorted(body.ordinals),
        "tasks": [
            {
                "task_id": sample["task_id"],
                "sha256": sample["sha256"],
                "arm": sample["arm"],
                "ordinal": sample["ordinal"],
            }
            for sample in selected
        ],
        "batch": {
            "id": str(batch.id),
            "name": batch.name,
            "display_id": batch.display_id,
        }
        if batch
        else None,
        "source_snapshots": before,
        "current_exclusions": history,
        "claim": "task_selection_only",
    }
    decision = Decision(
        id=uuid.uuid4(),
        round_id=round.id,
        actor_id=user.id,
        request_id=body.request_id,
        request_digest=digest,
        action=body.action,
        accepted_batch_id=batch.id if batch else None,
        snapshot=snapshot,
        snapshot_digest=content_digest(snapshot),
    )
    db.add(decision)
    await db.flush()
    for ordinal in body.ordinals:
        rows[ordinal].disposition = (
            "accepted" if body.action == "accept" else "rejected"
        )
        rows[ordinal].decision_id = decision.id
    await originals.check(db, context, commit=False)
    return decision
