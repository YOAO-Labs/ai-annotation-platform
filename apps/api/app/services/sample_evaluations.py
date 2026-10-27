"""Immutable evaluation contracts and the durable owner of evaluation attempts."""

from __future__ import annotations

from app.services import training_storage_io as storage_io
from app.services import training_write_accounting as write_accounting
from contextlib import nullcontext
from app.services import storage_access

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
    sample_evaluation_storage as owners,
    trained_models,
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


def engine_for(value):
    if (
        value.get("schema") == 2
        or normalize_task(value.get("task") or "detect") == "segment"
    ):
        from app.services import sample_evaluation_masks

        return sample_evaluation_masks
    return engine


async def frozen_mask_truth(project_id, version, member, source, classes, *, db):
    from app.services import sample_evaluation_masks as masks
    from training_contract.dataset import segmentation_labels

    result = []
    if version.manifest.get("schema") == 4:
        from app.services.training_dataset_analysis import read_instance_masks

        if not isinstance(member.get("instances"), list):
            raise ValueError("Frozen mask instance mapping is missing")
        if len(member["instances"]) > masks.MAX_TRUTH_PER_IMAGE:
            raise ValueError("Frozen mask instance count exceeds the evaluation budget")
        encoded_bytes = [instance.get("rle_bytes") for instance in member["instances"]]
        if (
            any(type(size) is not int or size < 0 for size in encoded_bytes)
            or sum(encoded_bytes) > masks.MAX_SAMPLE_RLE_BYTES
        ):
            raise ValueError("Frozen masks exceed the sample RLE budget")
        from app.services import training_dataset_storage

        source_owner = training_dataset_storage.resolve(version)
        retained_instances = await storage_io.read(
            db,
            source_owner,
            "sample_evaluation_source",
            lambda transport: read_instance_masks(
                project_id,
                version,
                SimpleNamespace(
                    included=True,
                    label_sha256=member["label_sha256"],
                    task_id=uuid.UUID(member["task_id"]),
                ),
                [
                    uuid.UUID(instance["annotation_id"])
                    for instance in member["instances"]
                ],
                transport=transport,
            ),
            object_key=source_owner.key(f"masks/{member['task_id']}.json"),
            sha256=member["label_sha256"],
        )
        for instance in member["instances"]:
            index = instance["class_index"]
            if type(index) is not int or not 0 <= index < len(classes):
                raise ValueError("Frozen mask class index is invalid")
            retained = retained_instances[instance["annotation_id"]]
            if (
                str(retained["annotation_id"]) != instance["annotation_id"]
                or retained["annotation_version"] != instance["annotation_version"]
                or retained["pixel_sha256"] != instance["pixel_sha256"]
            ):
                raise ValueError("Frozen mask instance source identity differs")
            result.append(
                {
                    "index": len(result),
                    "class_index": index,
                    "annotation_id": instance["annotation_id"],
                    "annotation_version": instance["annotation_version"],
                    "rasterizer": "native-rle-1",
                    **masks.mask_evidence(retained["rle"]),
                }
            )
    else:
        from app.services.mask_conversion import rasterize_region_geometry

        text, instances = segmentation_labels(source["annotations"], classes)
        if hashlib.sha256(text.encode()).hexdigest() != member["label_sha256"]:
            raise ValueError("Frozen polygon labels differ")
        annotations = {item["id"]: item for item in source["annotations"]}
        if (
            member["width"]
            * member["height"]
            * sum(
                len(annotation["geometry"]["points"])
                for annotation in annotations.values()
            )
            > 100_000_000
        ):
            raise ValueError(
                "Frozen polygon rasterization exceeds the evaluation operation budget"
            )
        for instance in instances:
            annotation = annotations[instance["annotation_id"]]
            rle = await asyncio.to_thread(
                rasterize_region_geometry,
                annotation["geometry"],
                member["width"],
                member["height"],
            )
            result.append(
                {
                    "index": len(result),
                    "class_index": instance["class_index"],
                    "annotation_id": instance["annotation_id"],
                    "annotation_version": instance["annotation_version"],
                    "rasterizer": masks.EVALUATOR["polygon_rasterizer"],
                    **masks.mask_evidence(rle),
                }
            )
    masks._checked(result, len(classes), False)
    if len({item["annotation_id"] for item in result}) != len(result) or any(
        item["rle"]["size"] != [member["height"], member["width"]] for item in result
    ):
        raise ValueError("Frozen mask source or original dimensions differ")
    return result


ACTIVE = {"queued", "running", "cancelling", "lost"}
TERMINAL = {"succeeded", "failed", "cancelled"}
SCOPES = frozenset({"datasets:read", "datasets:write", "annotations:read"})


def now():
    return datetime.now(timezone.utc)


def reject(code, message, status=409):
    return HTTPException(status_code=status, detail={"code": code, "message": message})


async def load_run(db, project_id, run_id, *, lock=False):
    query = select(Run).where(Run.id == run_id, Run.project_id == project_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    run = await db.scalar(query)
    if run is None:
        raise reject("evaluation_missing", "样本评估不存在", 404)
    return run


def checked_dataset(version, expected_task="detect"):
    if (
        version.status != "ready"
        or content_digest(version.snapshot) != version.snapshot_digest
    ):
        raise reject("dataset_unavailable", "冻结数据版本尚未就绪或快照校验失败")
    manifest = version.manifest
    try:
        contract = contract_of(manifest)
        if (
            normalize_task(contract["task"]) != expected_task
            or contract_of(version.snapshot) != contract
        ):
            raise reject("unsupported_task", "样本评估任务与冻结数据任务不一致", 422)
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
    selected_engine = engine_for(ref)
    segmentation = selected_engine is not engine
    task = "segment" if segmentation else "detect"
    version = await db.get(
        TrainingDatasetVersion, body.dataset_id, populate_existing=True
    )
    if not version or version.project_id != project.id:
        raise reject("dataset_missing", "冻结数据版本不存在", 404)
    manifest = checked_dataset(version, task)
    if profile["implementation"] != (
        "aap-ultralytics-instance-mask" if segmentation else "aap-ultralytics-detection"
    ):
        raise reject("profile_task_mismatch", "推理评估资格与数据任务不一致", 422)
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
        if (
            not 0 < member["size_bytes"] <= selected_engine.MAX_RAW_BYTES
            or member["width"] * member["height"] > selected_engine.MAX_PIXELS
            or (
                segmentation
                and max(member["width"], member["height"])
                > selected_engine.MAX_DIMENSION
            )
        ):
            raise reject("sample_budget", "所选图片超过评估大小或像素上限", 422)
        if segmentation:
            truth = await frozen_mask_truth(
                project.id, version, member, source, classes, db=db
            )
        else:
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
        if segmentation:
            samples[-1]["label_sha256"] = content_digest(
                sorted((item["class_index"], item["mask_sha256"]) for item in truth)
            )
    if body.role == "held_out_test":
        native_dataset = await db.get(
            TrainingDatasetVersion, uuid.UUID(ref["provenance"]["dataset_id"])
        )
        if not native_dataset:
            raise reject("candidate_dataset_missing", "候选训练数据身份不可核验")
        native_manifest = checked_dataset(native_dataset, task)
        used = {m["sha256"] for m in native_manifest["samples"]}
        if used & {m["sha256"] for m in samples}:
            raise reject(
                "test_contamination", "独立测试样本与候选训练或验证数据重合", 422
            )
    issued = int(time.time())
    spec = {
        "schema": 2 if segmentation else 1,
        "evaluation_id": str(evaluation_id),
        "model_ref": ref,
        "dataset_id": str(version.id),
        "snapshot_digest": version.snapshot_digest,
        "manifest_digest": manifest["content_digest"],
        "package_sha256": version.package_sha256,
        "benchmark_digest": selected_engine.benchmark_identity(samples, classes),
        "classes": classes,
        "native_to_benchmark": mapping,
        "role": body.role,
        "held_out_confirmed": body.held_out_confirmed,
        "confidence": body.confidence,
        "matching_iou": body.matching_iou,
        "nms_iou": body.nms_iou,
        "evaluator": selected_engine.EVALUATOR,
        "preprocessing": engine.PREPROCESSING,
        "profile": profile,
        "profile_digest": profile["profile_digest"],
        "samples": samples,
        "issued_at": issued,
        "expires_at": issued + 86400,
    }
    spec["comparison_digest"] = selected_engine.comparison_identity(spec)
    if segmentation:
        _bounded(spec, budget=[engine.REPORT_NODE_BUDGET // 2])
        if (
            len(json.dumps(spec, ensure_ascii=False, separators=(",", ":")).encode())
            > MAX_REPORT_BYTES // 2
        ):
            raise reject("mask_truth_budget", "冻结掩码真值超过有界报告预算", 422)
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
    try:
        await owners.allocate(db, run)
    except TrainingDatasetError as exc:
        raise reject(exc.code, str(exc), exc.status_code) from exc
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
    owners.resolve(run)
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
    selected_engine = engine_for(spec)
    if spec["schema"] == 2:
        for frozen in spec["samples"]:
            selected_engine._checked(frozen["truth"], len(spec["classes"]), False)
            if frozen["label_sha256"] != content_digest(
                sorted(
                    (item["class_index"], item["mask_sha256"])
                    for item in frozen["truth"]
                )
            ) or any(
                item["rle"]["size"] != [frozen["height"], frozen["width"]]
                for item in frozen["truth"]
            ):
                raise reject("spec_corrupt", "冻结掩码真值身份校验失败")
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
        != selected_engine.benchmark_identity(spec["samples"], spec["classes"])
        or spec["comparison_digest"] != selected_engine.comparison_identity(spec)
        or spec["evaluator"] != selected_engine.EVALUATOR
        or spec["preprocessing"] != engine.PREPROCESSING
        or spec["profile_digest"] != spec["profile"]["profile_digest"]
        or spec["profile_digest"]
        != content_digest(
            {k: v for k, v in spec["profile"].items() if k != "profile_digest"}
        )
    ):
        raise reject("spec_corrupt", "冻结评估规格校验失败")
    return spec


def prepare_input(run, version, frozen, *, transport=None):
    if frozen["size_bytes"] > engine.MAX_RAW_BYTES:
        raise ValueError("Frozen input exceeds byte budget")
    source, _content_type = read_media(
        run.project_id, version, SimpleNamespace(**frozen), transport=transport
    )
    try:
        raw = source.read(engine.MAX_RAW_BYTES + 1)
    finally:
        storage_access.close_registered(source)
    png, preprocessing = engine.normalize_image(
        raw, width=frozen["width"], height=frozen["height"]
    )
    if preprocessing["raw_sha256"] != frozen["sha256"]:
        raise ValueError("Frozen input digest differs")
    return png, preprocessing


def successful_evidence(run, attempt, row, result):
    spec = validate_spec(run)
    selected_engine = engine_for(spec)
    frozen = spec["samples"][row.ordinal]
    trained_models.validate_result_runtime(result.meta, spec["model_ref"])
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
    from app.services.controlled_segmentation import runtime_for_reference

    predictions = selected_engine.predictions_from_wire(
        result.result,
        spec["native_to_benchmark"],
        **(
            {
                "runtime_qualification": runtime_for_reference(
                    spec["model_ref"], spec["profile"]["ultralytics_version"]
                ),
                "metadata": result.meta,
                "width": frozen["width"],
                "height": frozen["height"],
            }
            if spec["schema"] == 2
            else {}
        ),
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
        "matching": selected_engine.evaluate_sample(
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
    selected_engine = engine_for(spec)
    samples = [
        copy.deepcopy(row.evidence) if row.evidence else incomplete_evidence(spec, row)
        for row in rows
    ]
    counts = {
        state: sum(s["status"] == state for s in samples)
        for state in ("succeeded", "failed", "pending")
    }
    report = {
        "schema": spec["schema"],
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
        "aggregate": selected_engine.aggregate(samples, spec["classes"])
        if status == "succeeded"
        else None,
        "samples": samples,
    }
    return verify_report(report, run, attempt, rows)


def verify_report(report, run, attempt, rows):
    """Verify against trusted DB facts before any index-based aggregation/filtering."""
    spec = validate_spec(run)
    selected_engine = engine_for(spec)
    checked = EvaluationReport.model_validate(report).model_dump(
        mode="json", by_alias=True
    )
    expected = {
        "schema": spec["schema"],
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
            not 0 <= pred["native_class_index"] < len(spec["native_to_benchmark"])
            or pred["class_index"]
            != spec["native_to_benchmark"][pred["native_class_index"]]
            for pred in sample["predictions"]
        ):
            raise ValueError("Report native class mapping differs")
        matching = selected_engine.evaluate_sample(
            sample["truth"],
            sample["predictions"],
            class_count=len(spec["classes"]),
            confidence=spec["confidence"],
            matching_iou=spec["matching_iou"],
        )
        if sample["matching"] != matching:
            raise ValueError("Report matching differs from the qualified evaluator")
    if checked["complete"] and checked["aggregate"] != selected_engine.aggregate(
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
    return owners.resolve(run).prefix


def upload_report(run, attempt, report, *, transport=None):
    transport = storage if transport is None else transport
    owner = owners.resolve(run)
    if attempt.run_id != run.id:
        raise TrainingDatasetError(
            "training_storage_invalid_key", "报告执行尝试不属于该评估", status_code=409
        )
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
    owners.expect_report(owner, attempt.id, artifact["key"], sha)
    accounting = getattr(transport, "_write_accounting", None)
    observation = (
        write_accounting.Observation(
            accounting,
            transport.client,
            artifact["key"],
            artifact,
        )
        if accounting is not None
        else None
    )
    with observation.hooks() if observation is not None else nullcontext():
        transport.client.put_object(
            Bucket=owner.bucket,
            Key=artifact["key"],
            Body=raw,
            ContentType="application/json",
            Metadata=report_metadata(run, attempt),
        )
    if (
        read_json_artifact(
            artifact,
            attempt.id,
            bucket=owner.bucket,
            transport=transport,
            required_metadata=report_metadata(run, attempt),
            node_budget=engine.REPORT_NODE_BUDGET,
            observation=observation,
        )
        != report
    ):
        raise ValueError("Uploaded evaluation report differs")
    if observation is not None:
        observation.complete()
    return artifact


async def read_report(db, run, attempt):
    if run.deletion_requested_at:
        raise reject("evaluation_deleting", "评估正在删除，报告已停止提供")
    if attempt.run_id != run.id:
        raise reject("report_corrupt", "报告执行尝试不属于该评估")
    if attempt.status not in TERMINAL or not attempt.report:
        raise reject("report_pending", "该执行尝试尚无已核验报告")
    artifact = attempt.report
    try:
        owner = owners.resolve(run)
        owners.expect_report(owner, attempt.id, artifact["key"], artifact["sha256"])
    except TrainingDatasetError as exc:
        raise reject(exc.code, str(exc), exc.status_code) from exc
    except (KeyError, TypeError) as exc:
        raise reject("report_corrupt", "报告存储身份不匹配") from exc
    try:
        report = await storage_io.read(
            db,
            owner,
            "sample_evaluation_read",
            lambda transport: read_json_artifact(
                artifact,
                attempt.id,
                bucket=owner.bucket,
                transport=transport,
                required_metadata=report_metadata(run, attempt),
                node_budget=engine.REPORT_NODE_BUDGET,
            ),
            object_key=artifact["key"],
            sha256=artifact["sha256"],
            size_bytes=artifact["size_bytes"],
        )
        if report.get("status") != attempt.status:
            raise ValueError("Report state differs from terminal attempt")
        return verify_report(report, run, attempt, await sample_rows(db, run))
    except Exception as exc:
        raise reject(
            "report_unavailable", "报告未通过完整性核验，暂不展示指标或错误证据"
        ) from exc


def claim_attempt(run, attempt, claim_id):
    run.status = attempt.status = "running"
    attempt.claim_id = claim_id
    attempt.heartbeat_at = attempt.started_at = now()


def heartbeat_attempt(attempt):
    attempt.heartbeat_at = now()


def checkpoint_sample(row, attempt, evidence, *, clear_errors: bool):
    """Cancellation recovery preserves existing diagnostics."""
    row.evidence, row.status, row.produced_attempt_id = (
        evidence,
        "succeeded",
        attempt.id,
    )
    if clear_errors:
        row.error_code = row.error_message = None


def fail_sample(row, *, code, message):
    row.status, row.error_code, row.error_message = "failed", code, message


def mark_sample_inflight(row, request):
    row.request, row.status = request, "inflight"


def mark_lost(run, attempt, *, code, message):
    run.status = attempt.status = "lost"
    run.error_code = attempt.error_code = code
    run.error_message = attempt.error_message = message


def set_error(run, attempt, code, message):
    run.error_code = attempt.error_code = code
    run.error_message = attempt.error_message = message


def release_claim(attempt):
    attempt.claim_id = None


def mark_dispatched(attempt):
    attempt.dispatched_at = now()


def mark_maintenance(attempt, timestamp):
    attempt.maintenance_at = timestamp


def request_deletion(run):
    run.deletion_requested_at = run.deletion_requested_at or now()


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
        mark_lost(
            run,
            attempt,
            code="execution_lost",
            message="评估执行失联；恢复前会核对旧请求收据",
        )
        await jobs.mark_failed(db, attempt.async_job_id, error=run.error_message)
        await notify_job_terminal(db, job_id=attempt.async_job_id)
    release_claim(attempt)


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
    run = await db.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not run or not run.deletion_requested_at or run.status not in TERMINAL:
        return False

    owner = owners.resolve(run)
    await guard_source(db, evaluation_id=run.id)
    await db.commit()
    await storage_io.run(
        db,
        owner,
        "sample_evaluation_cleanup",
        lambda transport: transport.delete_prefix(owner.prefix, bucket=owner.bucket),
    )
    run = await db.scalar(
        select(Run)
        .where(Run.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run and run.deletion_requested_at and run.status in TERMINAL:
        if owners.resolve(run).ref != owner.ref:
            raise TrainingDatasetError(
                "training_storage_unproven",
                "评估清理期间存储归属已变化",
                status_code=409,
            )
        await guard_source(db, evaluation_id=run.id)
        await db.delete(run)
        await db.commit()
    return True
