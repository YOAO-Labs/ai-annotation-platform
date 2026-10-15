"""Final best-weight evidence for one authorized, immutable execution attempt."""

import asyncio
import math

from fastapi import HTTPException

from app.schemas.training_run import TrainingEvaluationDescriptor
from app.services.training_artifacts import read_json_artifact
from app.services.training_dataset_bridge import content_digest

METRICS = {"mAP50-95", "mAP50", "mAP75", "precision", "recall", "AR100"}
SEMANTICS = dict(
    unit="fraction", direction="higher", minimum=0, maximum=1, unavailable_below=0
)


def metric_values(raw, framework, *, per_class=False):
    if not isinstance(raw, dict) or len(raw) > 100:
        raise ValueError("Invalid metric summary")
    values = {}
    for key, value in raw.items():
        name = key.removeprefix("metrics/").removesuffix("(B)")
        if name not in METRICS or (
            framework == "libreyolo" and name in {"precision", "recall"}
        ):
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise ValueError("Invalid metric value")
        if value > 1:
            raise ValueError("Fraction metric exceeds range")
        canonical = name if per_class else "metrics/" + name
        if canonical not in values or not key.endswith("(B)"):
            values[canonical] = float(value) if value >= 0 else None
    return values


def parse_evaluation(report, run, attempt, metrics, weights, offset, limit):
    framework = run.config["framework"]
    classes = run.config["classes"]
    legacy = report.get("evaluation", {})
    if report.get("validation_digest") != run.config["validation_digest"] or (
        legacy.get("framework") != framework
        or legacy.get("version") != run.config["framework_version"]
        or legacy.get("task") != "detection"
    ):
        raise ValueError("Report and frozen experiment identity conflict")
    if legacy.get("weights_sha256", weights["sha256"]) != weights["sha256"]:
        raise ValueError("Report weight identity conflict")
    summary = metric_values(report.get("summary", {}), framework)
    rows = report.get("per_class", [])
    if not isinstance(rows, list) or len(rows) > len(classes) or len(classes) > 10000:
        raise ValueError("Invalid per-class coverage")
    seen, per_class = set(), []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid per-class row")
        index = row.get("index")
        if (
            type(index) is not int
            or not 0 <= index < len(classes)
            or index in seen
            or row.get("name") != classes[index]
        ):
            raise ValueError("Per-class identity conflict")
        seen.add(index)
        per_class.append(
            dict(
                index=index,
                name=classes[index],
                metrics=metric_values(row, framework, per_class=True),
            )
        )
    per_class.sort(key=lambda row: row["index"])
    available = report.get("per_class_available", True)
    if type(available) is not bool or (not available and per_class):
        raise ValueError("Invalid per-class availability")
    descriptor, compatibility, reasons = None, None, []
    if "comparison" not in report:
        reasons.append(
            "原生评估器未记录实际 backend 身份，仅并列展示"
            if report.get("comparison_unavailable_reason") == "evaluator_not_recorded"
            else "旧报告未记录完整的权重与评估器身份，仅并列展示"
        )
    else:
        descriptor = TrainingEvaluationDescriptor.model_validate(report["comparison"])
        d = descriptor.model_dump(mode="json")
        if (
            d["config_digest"] != run.config_digest
            or d["candidate"]["weights_sha256"] != weights["sha256"]
            or d["benchmark"]["validation_digest"] != run.config["validation_digest"]
            or d["benchmark"]["classes"] != classes
            or d["evaluator"]["framework"] != framework
            or d["evaluator"]["version"] != run.config["framework_version"]
            or d["candidate"]["best_epoch"] is not None
            and d["candidate"]["best_epoch"] > run.config["parameters"]["epochs"]
        ):
            raise ValueError("Evaluation descriptor does not bind frozen inputs")
        options = d["evaluator"]["options"]
        required = {
            "split",
            "imgsz",
            "batch",
            "conf",
            "iou",
            "max_det",
            "half",
            "augment",
            "device",
            "workers",
        }
        required |= (
            {"rect", "single_cls", "classes", "agnostic_nms"}
            if framework == "ultralytics"
            else {"eval_max_det", "faster_coco_eval"}
        )
        if (
            not required <= options.keys()
            or options["split"] != "val"
            or options["imgsz"] != run.config["parameters"]["imgsz"]
        ):
            raise ValueError("Incomplete actual evaluator options")
        if any(
            k not in d["semantics"] or d["semantics"][k] != SEMANTICS for k in summary
        ) or any(
            k.removeprefix("metrics/") not in METRICS or semantics != SEMANTICS
            for k, semantics in d["semantics"].items()
        ):
            raise ValueError("Metric semantics conflict")
        compatibility = content_digest(
            {
                "benchmark": d["benchmark"],
                "evaluator": d["evaluator"],
                "semantics": d["semantics"],
                "observed_classes": sorted(seen),
            }
        )
    return dict(
        identity_status="verified" if descriptor else "unknown",
        reasons=reasons,
        descriptor=descriptor,
        compatibility_digest=compatibility,
        summary=summary,
        per_class_available=available,
        per_class_total=len(per_class),
        per_class=per_class[offset : offset + limit],
        provenance=dict(
            run_id=str(run.id),
            attempt_id=str(attempt.id),
            config_digest=run.config_digest,
            report_sha256=metrics["sha256"],
            weights_sha256=weights["sha256"],
            runtime=attempt.runtime,
        ),
    )


async def read_evaluation(run, attempt, offset, limit):
    if attempt.status != "succeeded":
        raise HTTPException(status_code=409, detail="本次执行尚无成功发布的最终评估")
    artifacts = list(attempt.artifacts.values())
    metrics = [a for a in artifacts if a.get("role") == "metrics"]
    weights = [a for a in artifacts if a.get("role") == "weights"]
    if len(metrics) != 1 or len(weights) != 1:
        raise HTTPException(
            status_code=409, detail="本次执行缺少唯一的已验证评估与最佳权重"
        )
    try:
        report = await asyncio.to_thread(read_json_artifact, metrics[0], attempt.id)
        return parse_evaluation(
            report, run, attempt, metrics[0], weights[0], offset, limit
        )
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail="最终评估报告缺失、损坏或与本次执行身份不符"
        ) from exc
