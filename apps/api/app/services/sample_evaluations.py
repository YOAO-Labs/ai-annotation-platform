"""Immutable evaluation contracts and the durable owner of evaluation attempts."""

from __future__ import annotations

from aap_task_contract import normalize_task

import asyncio
import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import func, select

from app.db.models.sample_evaluation import (
    SampleEvaluationAttempt as Attempt,
    SampleEvaluationRun as Run,
    SampleEvaluationSample as Sample,
)
from app.db.models.training_dataset import TrainingDatasetVersion
from app.schemas.sample_evaluation import (
    EvaluationReport,
    EvaluationSampleResult,
    EvaluationSpec,
)
from app.services import (
    async_job as jobs,
    sample_evaluation_engine as engine,
)
from app.services.active_learning_references import guard_source
from app.services.async_job_notify import notify_job_terminal
from app.services.managed_inference import (
    qualification as qualification,
    request_identity as request_identity,
    verify_inference as verify_inference,
)
from app.services.storage import storage_service as storage
from app.services.training_artifacts import (
    MAX_REPORT_BYTES,
    read_json_artifact,
    _bounded,
)
from app.services.training_dataset_analysis import read_media
from app.services.training_dataset_bridge import (
    content_digest,
    detection_labels,
    contract_of,
    TrainingDatasetError,
)
from app.services.training_datasets import assert_training_authority

ACTIVE = {"queued", "running", "cancelling", "lost"}
TERMINAL = {"succeeded", "failed", "cancelled"}
SCOPES = frozenset({"datasets:read", "datasets:write", "annotations:read"})


async def assert_no_project_evaluations(db, project_id: uuid.UUID) -> None:
    if await db.scalar(select(Run.id).where(Run.project_id == project_id).limit(1)):
        raise HTTPException(
            status_code=409,
            detail="项目包含冻结样本评估，请先结束并删除评估",
        )


def now():
    return datetime.now(timezone.utc)


def reject(code, message, status=409):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def authority(db, run):
    await assert_training_authority(
        db,
        project_id=run.project_id,
        actor_id=run.creator_id,
        api_key_id=run.authorization.get("api_key_id"),
        required_scopes=SCOPES,
    )


async def load_run(db, project_id, run_id, *, lock=False):
    query = select(Run).where(Run.id == run_id, Run.project_id == project_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    run = await db.scalar(query)
    if run is None:
        raise reject("evaluation_missing", "样本评估不存在", 404)
    return run


def checked_dataset(version):
    if (
        version.status != "ready"
        or content_digest(version.snapshot) != version.snapshot_digest
    ):
        raise reject("dataset_unavailable", "冻结数据版本尚未就绪或快照校验失败")
    manifest = version.manifest
    try:
        contract = contract_of(manifest)
        if (
            normalize_task(contract["task"]) != "detect"
            or contract_of(version.snapshot) != contract
        ):
            raise reject("unsupported_task", "样本评估目前仅通过图片检测资格验证", 422)
    except TrainingDatasetError as exc:
        raise reject("unsupported_task", str(exc), 422) from exc
    if (
        not manifest.get("content_digest")
        or content_digest({k: v for k, v in manifest.items() if k != "content_digest"})
        != manifest["content_digest"]
    ):
        raise reject("dataset_corrupt", "冻结清单校验失败")
    if (
        version.snapshot.get("classes") != manifest.get("classes")
        or not version.package_sha256
    ):
        raise reject("dataset_corrupt", "冻结类别或训练包身份不完整")
    return manifest


async def frozen_spec(db, project, body, evaluation_id, ref, profile):
    version = await db.get(
        TrainingDatasetVersion, body.dataset_id, populate_existing=True
    )
    if not version or version.project_id != project.id:
        raise reject("dataset_missing", "冻结数据版本不存在", 404)
    manifest = checked_dataset(version)
    classes = manifest["classes"]
    if (
        len(ref["classes"]) != len(classes)
        or len(set(ref["classes"])) != len(classes)
        or any(
            not isinstance(name, str) or not 0 < len(name) <= 120
            for name in ref["classes"]
        )
    ):
        raise reject(
            "native_classes_unsupported",
            "原生类别数量、名称或顺序身份不符合评估上限",
            422,
        )
    if len(ref["bindings"]) != len(classes) or set(ref["bindings"]) != set(classes):
        raise reject("ambiguous_binding", "模型类别须与冻结类别一一对应", 422)
    mapping = [classes.index(name) for name in ref["bindings"]]
    if len(set(mapping)) != len(classes):
        raise reject("ambiguous_binding", "模型类别绑定存在重复", 422)
    members = {str(m["task_id"]): m for m in manifest["samples"]}
    sources = {str(s["task_id"]): s for s in version.snapshot["samples"]}
    samples = []
    for task_id in body.sample_task_ids:
        member, source = members.get(str(task_id)), sources.get(str(task_id))
        if not member or not source or member.get("split") != "val":
            raise reject("sample_unavailable", "须明确选择该版本的验证分区成员", 422)
        text = detection_labels(source["annotations"], classes)
        if hashlib.sha256(text.encode()).hexdigest() != member.get("label_sha256"):
            raise reject("labels_corrupt", "冻结标签摘要不匹配")
        truth = engine.truth_from_yolo(text, len(classes))
        samples.append(
            {
                "ordinal": len(samples),
                **{
                    key: member[key]
                    for key in (
                        "task_id",
                        "sha256",
                        "label_sha256",
                        "width",
                        "height",
                        "size_bytes",
                        "split",
                    )
                },
                "explicit_negative": (
                    True
                    if source.get("eligibility", {}).get("negative") is True
                    else None
                )
                if not truth
                else False,
                "truth": truth,
            }
        )
        if (
            not 0 < member["size_bytes"] <= engine.MAX_RAW_BYTES
            or member["width"] * member["height"] > engine.MAX_PIXELS
        ):
            raise reject("sample_budget", "所选图片超过评估大小或像素上限", 422)
    if body.role == "held_out_test":
        native_dataset = await db.get(
            TrainingDatasetVersion, uuid.UUID(ref["provenance"]["dataset_id"])
        )
        if not native_dataset:
            raise reject("candidate_dataset_missing", "候选训练数据身份不可核验")
        native_manifest = checked_dataset(native_dataset)
        used = {m["sha256"] for m in native_manifest["samples"]}
        if used & {m["sha256"] for m in samples}:
            raise reject(
                "test_contamination", "独立测试样本与候选训练或验证数据重合", 422
            )
    issued = int(time.time())
    spec = {
        "schema": 1,
        "evaluation_id": str(evaluation_id),
        "model_ref": ref,
        "dataset_id": str(version.id),
        "snapshot_digest": version.snapshot_digest,
        "manifest_digest": manifest["content_digest"],
        "package_sha256": version.package_sha256,
        "benchmark_digest": engine.benchmark_identity(samples, classes),
        "classes": classes,
        "native_to_benchmark": mapping,
        "role": body.role,
        "held_out_confirmed": body.held_out_confirmed,
        "confidence": body.confidence,
        "matching_iou": body.matching_iou,
        "nms_iou": body.nms_iou,
        "evaluator": engine.EVALUATOR,
        "preprocessing": engine.PREPROCESSING,
        "profile": profile,
        "profile_digest": profile["profile_digest"],
        "samples": samples,
        "issued_at": issued,
        "expires_at": issued + 86400,
    }
    spec["comparison_digest"] = engine.comparison_identity(spec)
    return EvaluationSpec.model_validate(spec).model_dump(mode="json", by_alias=True)


async def create_run(
    db, project, user, body, *, evaluation_id, request_digest, spec, api_key_id=None
):
    run = Run(
        id=evaluation_id,
        project_id=project.id,
        creator_id=user.id,
        dataset_id=body.dataset_id,
        model_version_id=body.model_version_id,
        deployment_id=body.deployment_id,
        request_id=body.request_id,
        request_digest=request_digest,
        name=body.name,
        spec=spec,
        spec_digest=content_digest(spec),
        authorization={"api_key_id": api_key_id},
        generation=0,
    )
    db.add(run)
    await db.flush()
    for frozen in spec["samples"]:
        db.add(
            Sample(
                run_id=run.id,
                ordinal=frozen["ordinal"],
                request_id=uuid.uuid5(run.id, f"sample:{frozen['ordinal']}"),
            )
        )
    await new_attempt(db, run)
    return run


async def retry_run(db, run):
    previous = await db.get(Attempt, run.current_attempt_id)
    previous.claim_id = None
    if previous.status == "lost":
        previous.completed_at = now()
        await jobs.mark_failed(
            db,
            previous.async_job_id,
            error="旧执行已被新尝试接管；后端请求仍通过同一把锁防止重叠",
        )
    for row in await sample_rows(db, run):
        if row.status == "failed":
            row.status, row.error_code, row.error_message = "pending", None, None
    await new_attempt(db, run)


async def new_attempt(db, run):
    run.generation += 1
    ordinal = (
        await db.scalar(
            select(func.max(Attempt.ordinal)).where(Attempt.run_id == run.id)
        )
        or 0
    ) + 1
    attempt = Attempt(run_id=run.id, ordinal=ordinal, generation=run.generation)
    db.add(attempt)
    await db.flush()
    job = await jobs.create_job(
        db,
        kind="sample_evaluation",
        user_id=run.creator_id,
        project_id=run.project_id,
        payload={
            "evaluation_id": str(run.id),
            "attempt_id": str(attempt.id),
            "name": run.name,
        },
    )
    run.current_attempt_id, run.async_job_id = attempt.id, job.id
    attempt.async_job_id = job.id
    run.status = "queued"
    run.error_code = run.error_message = run.completed_at = None
    return attempt


async def sample_rows(db, run):
    return list(
        (
            await db.scalars(
                select(Sample).where(Sample.run_id == run.id).order_by(Sample.ordinal)
            )
        ).all()
    )


def validate_spec(run):
    spec = EvaluationSpec.model_validate(run.spec).model_dump(
        mode="json", by_alias=True
    )
    if (
        content_digest(spec) != run.spec_digest
        or spec["evaluation_id"] != str(run.id)
        or spec["dataset_id"] != str(run.dataset_id)
        or spec["model_ref"].get("model_version_id") != str(run.model_version_id)
        or spec["model_ref"].get("deployment_id") != str(run.deployment_id)
        or spec["model_ref"].get("project_id") != str(run.project_id)
        or spec["model_ref"].get("reference_digest")
        != content_digest(
            {k: v for k, v in spec["model_ref"].items() if k != "reference_digest"}
        )
        or spec["benchmark_digest"]
        != engine.benchmark_identity(spec["samples"], spec["classes"])
        or spec["comparison_digest"] != engine.comparison_identity(spec)
        or spec["evaluator"] != engine.EVALUATOR
        or spec["preprocessing"] != engine.PREPROCESSING
        or spec["profile_digest"] != spec["profile"]["profile_digest"]
        or spec["profile_digest"]
        != content_digest(
            {k: v for k, v in spec["profile"].items() if k != "profile_digest"}
        )
    ):
        raise reject("spec_corrupt", "冻结评估规格校验失败")
    return spec


def prepare_input(run, version, frozen):
    if frozen["size_bytes"] > engine.MAX_RAW_BYTES:
        raise ValueError("Frozen input exceeds byte budget")
    source, _content_type = read_media(
        run.project_id, version, SimpleNamespace(**frozen)
    )
    try:
        raw = source.read(engine.MAX_RAW_BYTES + 1)
    finally:
        source.close()
    png, preprocessing = engine.normalize_image(
        raw, width=frozen["width"], height=frozen["height"]
    )
    if preprocessing["raw_sha256"] != frozen["sha256"]:
        raise ValueError("Frozen input digest differs")
    return png, preprocessing


def successful_evidence(run, attempt, row, result):
    spec = validate_spec(run)
    frozen = spec["samples"][row.ordinal]
    inference = (result.meta or {}).get("sample_evaluation")
    verify_inference(spec, row, inference)
    identity = {
        k: spec["model_ref"][k]
        for k in ("model_version_id", "sha256", "classes_digest")
    }
    if (
        str(result.task_id) != frozen["task_id"]
        or (result.meta or {}).get("trained_model") != identity
    ):
        raise ValueError("Prediction candidate or task identity differs")
    predictions = engine.predictions_from_wire(
        result.result, spec["native_to_benchmark"]
    )
    evidence = {
        **{
            key: frozen[key]
            for key in (
                "ordinal",
                "task_id",
                "sha256",
                "label_sha256",
                "width",
                "height",
                "truth",
            )
        },
        "status": "succeeded",
        "predictions": predictions,
        "matching": engine.evaluate_sample(
            frozen["truth"],
            predictions,
            class_count=len(spec["classes"]),
            confidence=spec["confidence"],
            matching_iou=spec["matching_iou"],
        ),
        "derived_sha256": row.request["evaluation"]["input_sha256"],
        "preprocessing": row.request["preprocessing"],
        "request_id": str(row.request_id),
        "binding_digest": row.request["evaluation"]["binding_digest"],
        "inference": inference,
        "produced_attempt_id": str(attempt.id),
        "error_code": None,
        "error_message": None,
    }
    evidence["evidence_digest"] = content_digest(evidence)
    return EvaluationSampleResult.model_validate(evidence).model_dump(mode="json")


def incomplete_evidence(spec, row):
    frozen = spec["samples"][row.ordinal]
    return EvaluationSampleResult.model_validate(
        {
            **{
                key: frozen[key]
                for key in (
                    "ordinal",
                    "task_id",
                    "sha256",
                    "label_sha256",
                    "width",
                    "height",
                    "truth",
                )
            },
            "status": "failed" if row.status == "failed" else "pending",
            "request_id": row.request_id,
            "error_code": row.error_code,
            "error_message": row.error_message,
        }
    ).model_dump(mode="json")


def build_report(run, attempt, rows, status):
    spec = validate_spec(run)
    samples = [
        copy.deepcopy(row.evidence) if row.evidence else incomplete_evidence(spec, row)
        for row in rows
    ]
    counts = {
        state: sum(s["status"] == state for s in samples)
        for state in ("succeeded", "failed", "pending")
    }
    report = {
        "schema": 1,
        "evaluation_id": str(run.id),
        "attempt_id": str(attempt.id),
        "spec_digest": run.spec_digest,
        "benchmark_digest": spec["benchmark_digest"],
        "comparison_digest": spec["comparison_digest"],
        "model_ref": spec["model_ref"],
        "evaluator": spec["evaluator"],
        "status": status,
        "complete": status == "succeeded",
        "total_samples": len(samples),
        **{f"{key}_samples": value for key, value in counts.items()},
        "aggregate": engine.aggregate(samples, spec["classes"])
        if status == "succeeded"
        else None,
        "samples": samples,
    }
    return verify_report(report, run, attempt, rows)


def verify_report(report, run, attempt, rows):
    """Verify against trusted DB facts before any index-based aggregation/filtering."""
    spec = validate_spec(run)
    checked = EvaluationReport.model_validate(report).model_dump(
        mode="json", by_alias=True
    )
    expected = {
        "evaluation_id": str(run.id),
        "attempt_id": str(attempt.id),
        "spec_digest": run.spec_digest,
        "benchmark_digest": spec["benchmark_digest"],
        "comparison_digest": spec["comparison_digest"],
        "model_ref": spec["model_ref"],
        "evaluator": spec["evaluator"],
        "total_samples": len(spec["samples"]),
    }
    if any(checked[k] != v for k, v in expected.items()) or len(rows) != len(
        spec["samples"]
    ):
        raise ValueError("Report identity differs from frozen evaluation")
    for sample, row, frozen in zip(
        checked["samples"], rows, spec["samples"], strict=True
    ):
        if (
            row.ordinal != frozen["ordinal"]
            or str(row.request_id) != sample["request_id"]
            or any(
                sample[key] != frozen[key]
                for key in (
                    "ordinal",
                    "task_id",
                    "sha256",
                    "label_sha256",
                    "width",
                    "height",
                    "truth",
                )
            )
        ):
            raise ValueError("Report member differs from frozen sample")
        if sample["status"] != "succeeded":
            continue
        if (
            row.evidence != sample
            or str(row.produced_attempt_id) != sample["produced_attempt_id"]
            or not row.request
        ):
            raise ValueError("Report evidence has no trusted execution checkpoint")
        if sample["evidence_digest"] != content_digest(
            {k: v for k, v in sample.items() if k != "evidence_digest"}
        ):
            raise ValueError("Report sample evidence digest differs")
        if (
            sample["preprocessing"] != row.request["preprocessing"]
            or sample["derived_sha256"] != row.request["evaluation"]["input_sha256"]
            or sample["binding_digest"] != row.request["evaluation"]["binding_digest"]
            or row.request != request_identity(spec, row, sample["preprocessing"])
        ):
            raise ValueError("Report input/request identity differs")
        preprocessing = sample["preprocessing"]
        if (
            preprocessing.get("raw_sha256") != frozen["sha256"]
            or preprocessing.get("derived_sha256") != sample["derived_sha256"]
            or preprocessing.get("preprocessing_digest")
            != content_digest(engine.PREPROCESSING)
            or preprocessing.get("width") != frozen["width"]
            or preprocessing.get("height") != frozen["height"]
        ):
            raise ValueError("Report preprocessing identity differs")
        verify_inference(spec, row, sample["inference"])
        if any(
            pred["class_index"]
            != spec["native_to_benchmark"][pred["native_class_index"]]
            for pred in sample["predictions"]
        ):
            raise ValueError("Report native class mapping differs")
        matching = engine.evaluate_sample(
            sample["truth"],
            sample["predictions"],
            class_count=len(spec["classes"]),
            confidence=spec["confidence"],
            matching_iou=spec["matching_iou"],
        )
        if sample["matching"] != matching:
            raise ValueError("Report matching differs from the qualified evaluator")
    if checked["complete"] and checked["aggregate"] != engine.aggregate(
        checked["samples"], spec["classes"]
    ):
        raise ValueError("Report aggregate differs from qualified sample evidence")
    return checked


def report_metadata(run, attempt):
    return {
        "attempt-id": str(attempt.id),
        "evaluation-id": str(run.id),
        "spec-digest": run.spec_digest,
        "benchmark-digest": run.spec["benchmark_digest"],
        "weight-sha256": run.spec["model_ref"]["sha256"],
    }


def report_prefix(run):
    return f"projects/{run.project_id}/sample-evaluations/{run.id}/"


def upload_report(run, attempt, report):
    _bounded(report, budget=[engine.REPORT_NODE_BUDGET])
    raw = json.dumps(
        report, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode()
    if not 0 < len(raw) <= MAX_REPORT_BYTES:
        raise ValueError("Evaluation report exceeds byte budget")
    sha = hashlib.sha256(raw).hexdigest()
    artifact = {
        "key": f"{report_prefix(run)}{attempt.id}/{sha}.json",
        "sha256": sha,
        "size_bytes": len(raw),
    }
    storage.client.put_object(
        Bucket=storage.training_bucket,
        Key=artifact["key"],
        Body=raw,
        ContentType="application/json",
        Metadata=report_metadata(run, attempt),
    )
    if (
        read_json_artifact(
            artifact,
            attempt.id,
            required_metadata=report_metadata(run, attempt),
            node_budget=engine.REPORT_NODE_BUDGET,
        )
        != report
    ):
        raise ValueError("Uploaded evaluation report differs")
    return artifact


async def read_report(db, run, attempt):
    if run.deletion_requested_at:
        raise reject("evaluation_deleting", "评估正在删除，报告已停止提供")
    if attempt.status not in TERMINAL or not attempt.report:
        raise reject("report_pending", "该执行尝试尚无已核验报告")
    artifact = attempt.report
    if not artifact.get("key", "").startswith(f"{report_prefix(run)}{attempt.id}/"):
        raise reject("report_corrupt", "报告存储身份不匹配")
    try:
        report = await asyncio.to_thread(
            read_json_artifact,
            artifact,
            attempt.id,
            required_metadata=report_metadata(run, attempt),
            node_budget=engine.REPORT_NODE_BUDGET,
        )
        if report.get("status") != attempt.status:
            raise ValueError("Report state differs from terminal attempt")
        return verify_report(report, run, attempt, await sample_rows(db, run))
    except Exception as exc:
        raise reject(
            "report_unavailable", "报告未通过完整性核验，暂不展示指标或错误证据"
        ) from exc


async def mark_terminal(
    db, run, attempt, status, *, code=None, message=None, report=None
):
    run.status = attempt.status = status
    run.completed_at = attempt.completed_at = now()
    run.error_code = attempt.error_code = code
    run.error_message = attempt.error_message = message
    attempt.report = report
    if status == "succeeded":
        await jobs.mark_complete(
            db,
            attempt.async_job_id,
            result={"evaluation_id": str(run.id), "attempt_id": str(attempt.id)},
        )
    elif status == "cancelled":
        await jobs.mark_cancelled(db, attempt.async_job_id)
    else:
        await jobs.mark_failed(db, attempt.async_job_id, error=message or "评估失败")
    await notify_job_terminal(db, job_id=attempt.async_job_id)


async def request_job_cancel(db, job_id):
    """Resolve the current evaluation for this job before requesting cancellation."""
    run = await db.scalar(
        select(Run).where(Run.async_job_id == job_id).with_for_update()
    )
    if run is None:
        raise HTTPException(status_code=409, detail="该任务已被新的评估尝试替代")
    await request_cancel(db, run)


async def request_cancel(db, run):
    if run.status in TERMINAL or run.status == "cancelling":
        return
    attempt = await db.get(Attempt, run.current_attempt_id)
    run.status = attempt.status = "cancelling"
    run.error_code = attempt.error_code = "cancel_requested"
    run.error_message = attempt.error_message = "已请求取消，正在确认旧推理已停止"


async def signal_interrupted(db, job, *, cancelled):
    run = await db.scalar(
        select(Run).where(Run.async_job_id == job.id).with_for_update()
    )
    if not run or run.status in TERMINAL:
        return
    attempt = await db.get(Attempt, run.current_attempt_id)
    if cancelled or run.status == "cancelling":
        await request_cancel(db, run)
    else:
        run.status = attempt.status = "lost"
        run.error_code = attempt.error_code = "execution_lost"
        run.error_message = attempt.error_message = (
            "评估执行失联；恢复前会核对旧请求收据"
        )
        await jobs.mark_failed(db, attempt.async_job_id, error=run.error_message)
        await notify_job_terminal(db, job_id=attempt.async_job_id)
    attempt.claim_id = None


async def output(db, run):
    attempts = (
        await db.scalars(
            select(Attempt)
            .where(Attempt.run_id == run.id)
            .order_by(Attempt.ordinal.desc())
        )
    ).all()
    rows = await sample_rows(db, run)
    from app.schemas.sample_evaluation import EvaluationAttemptOut, SampleEvaluationOut

    return SampleEvaluationOut(
        **{
            key: getattr(run, key)
            for key in (
                "id",
                "project_id",
                "name",
                "model_version_id",
                "deployment_id",
                "dataset_id",
                "request_id",
                "status",
                "spec_digest",
                "spec",
                "current_attempt_id",
                "async_job_id",
                "error_code",
                "error_message",
                "created_at",
                "completed_at",
                "deletion_requested_at",
            )
        },
        benchmark_digest=run.spec["benchmark_digest"],
        comparison_digest=run.spec["comparison_digest"],
        progress={
            state: sum(row.status == state for row in rows)
            for state in ("pending", "inflight", "succeeded", "failed")
        },
        attempts=[EvaluationAttemptOut.model_validate(a) for a in attempts],
    )


async def purge_requested(db, run_id):
    run = await db.get(Run, run_id)
    if not run or not run.deletion_requested_at or run.status not in TERMINAL:
        return False

    await guard_source(db, evaluation_id=run.id)
    prefix = report_prefix(run)
    await db.commit()
    await asyncio.to_thread(
        storage.delete_prefix, prefix, bucket=storage.training_bucket
    )
    run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
    if run and run.deletion_requested_at and run.status in TERMINAL:
        await db.delete(run)
        await db.commit()
    return True
