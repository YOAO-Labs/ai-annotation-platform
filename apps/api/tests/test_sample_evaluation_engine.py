"""Hand-checkable detection semantics and frozen image coordinate qualification."""

import copy
import hashlib
import io
import json
import uuid

import pytest
from PIL import Image, ImageOps
from pydantic import ValidationError

from app.schemas.sample_evaluation import EvaluationReport, SampleEvaluationCreate
from app.services import sample_evaluation_engine as engine
from app.services.training_artifacts import MAX_REPORT_BYTES, _bounded
from app.services.training_dataset_bridge import content_digest


def truth(*items):
    return [
        {"index": i, "class_index": cls, "xyxy": list(box)}
        for i, (cls, box) in enumerate(items)
    ]


def predictions(*items):
    return [
        {
            "index": i,
            "class_index": cls,
            "native_class_index": cls,
            "xyxy": list(box),
            "score": score,
        }
        for i, (cls, score, box) in enumerate(items)
    ]


FULL = (0.0, 0.0, 1.0, 1.0)
HALF = (0.0, 0.0, 0.5, 1.0)


def evaluate(gt, pred, **kwargs):
    return engine.evaluate_sample(
        gt, pred, class_count=2, confidence=0.25, matching_iou=0.5, **kwargs
    )


def sample(gt, pred):
    return {
        "status": "succeeded",
        "truth": gt,
        "predictions": pred,
        "matching": evaluate(gt, pred),
    }


def test_duplicate_predictions_consume_each_truth_only_once_in_confidence_order():
    result = evaluate(truth((0, FULL)), predictions((0, 0.8, FULL), (0, 0.9, FULL)))
    assert (result["tp"], result["fp"], result["fn"]) == (1, 1, 0)
    assert result["matches"] == [{"prediction_index": 1, "truth_index": 0, "iou": 1.0}]
    assert result["false_positive_indices"] == [0]
    assert result["precision"] == 0.5 and result["recall"] == 1.0
    assert result["f1"] == pytest.approx(2 / 3)


def test_matching_and_confidence_boundaries_are_inclusive_without_epsilon():
    result = evaluate(truth((0, FULL)), predictions((0, 0.25, HALF)))
    assert result["matches"][0]["iou"] == 0.5
    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 0)
    below_score = evaluate(truth((0, FULL)), predictions((0, 0.249999999, FULL)))
    assert below_score["excluded_prediction_indices"] == [0]
    assert (below_score["tp"], below_score["fp"], below_score["fn"]) == (0, 0, 1)
    below_iou = evaluate(
        truth((0, FULL)), predictions((0, 0.8, (0.0, 0.0, 0.499999999, 1.0)))
    )
    assert (below_iou["tp"], below_iou["fp"], below_iou["fn"]) == (0, 1, 1)


def test_greedy_order_and_equal_iou_ties_are_explicit_not_global_assignment():
    gt = truth((0, (0.0, 0.0, 0.75, 1.0)), (0, (0.25, 0.0, 1.0, 1.0)))
    pred = predictions(
        (0, 0.9, (0.125, 0.0, 0.875, 1.0)), (0, 0.8, (0.0, 0.0, 0.5, 1.0))
    )
    result = evaluate(gt, pred)
    assert (result["tp"], result["fp"], result["fn"]) == (1, 1, 1)
    assert result["matches"] == [
        {"prediction_index": 0, "truth_index": 0, "iou": 5 / 7}
    ]
    gt = truth((0, FULL), (0, FULL))
    result = evaluate(gt, predictions((0, 0.8, FULL), (0, 0.8, FULL)))
    assert [(p["prediction_index"], p["truth_index"]) for p in result["matches"]] == [
        (0, 0),
        (1, 1),
    ]


@pytest.mark.parametrize(
    "gt,pred,counts,precision,recall,f1",
    [
        ([], [], (0, 0, 0), None, None, None),
        ([], predictions((0, 0.8, FULL)), (0, 1, 0), 0.0, None, 0.0),
        (truth((0, FULL)), [], (0, 0, 1), None, 0.0, 0.0),
        (truth((0, FULL)), predictions((1, 0.8, FULL)), (0, 1, 1), 0.0, 0.0, 0.0),
    ],
)
def test_negatives_missing_predictions_and_wrong_class_are_not_fabricated_perfect_scores(
    gt, pred, counts, precision, recall, f1
):
    result = evaluate(gt, pred)
    assert (result["tp"], result["fp"], result["fn"]) == counts
    assert (result["precision"], result["recall"], result["f1"]) == (
        precision,
        recall,
        f1,
    )


def test_confusion_and_localization_diagnostics_never_relabel_primary_errors():
    gt = truth((0, HALF), (1, (0.5, 0.0, 1.0, 1.0)))
    pred = predictions((1, 0.9, HALF), (1, 0.8, (0.5, 0.0, 0.625, 1.0)), (1, 0.7, HALF))
    result = evaluate(gt, pred)
    assert (result["tp"], result["fp"], result["fn"]) == (0, 3, 2)
    assert result["diagnostics"] == [
        {"kind": "confusion", "prediction_index": 0, "truth_index": 0, "iou": 1.0},
        {"kind": "localization", "prediction_index": 1, "truth_index": 1, "iou": 0.25},
    ]
    assert len({p["truth_index"] for p in result["diagnostics"]}) == 2


def test_per_class_counts_retain_class_direction_and_occurrence_multiplicity():
    evidence = sample(truth((0, HALF)), predictions((1, 0.8, HALF)))
    summary = engine.aggregate([evidence, evidence], ["car", "person"])
    assert (summary["tp"], summary["fp"], summary["fn"]) == (0, 2, 2)
    assert summary["per_class"][0]["fn"] == 2 and summary["per_class"][0]["fp"] == 0
    assert summary["per_class"][1]["fp"] == 2 and summary["per_class"][1]["fn"] == 0
    assert summary["confusion"] == [
        {"truth_class_index": 0, "prediction_class_index": 1, "count": 2}
    ]
    with pytest.raises(ValueError, match="Incomplete"):
        engine.aggregate([{"status": "failed"}], ["car"])


def test_filters_select_evidence_without_recomputing_matches_or_giving_fn_a_score():
    evidence = sample(
        truth((0, FULL), (1, FULL)), predictions((0, 0.8, FULL), (0, 0.4, FULL))
    )
    before = copy.deepcopy(evidence)
    assert engine.matches_filter(
        evidence, error="false_positive", confidence_min=0.4, confidence_max=0.4
    )
    assert not engine.matches_filter(
        evidence, error="false_positive", confidence_min=0.4000001
    )
    assert engine.matches_filter(
        evidence, error="false_negative", class_index=1, confidence_min=1.0
    )
    assert not engine.matches_filter(evidence, error="false_negative", class_index=0)
    assert engine.matches_filter(evidence, class_index=1)
    assert not engine.matches_filter(evidence, class_index=1, confidence_min=0.0)
    assert evidence == before
    assert engine.matches_filter({"status": "failed"}, error="failed")
    assert not engine.matches_filter({"status": "pending"}, error="failed")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, "0.5", None])
def test_invalid_numeric_evidence_is_rejected(value):
    pred = predictions((0, 0.8, FULL))
    pred[0]["score"] = value
    with pytest.raises(ValueError):
        evaluate(truth((0, FULL)), pred)


@pytest.mark.parametrize(
    "box",
    [
        [0, 0, 0, 1],
        [-0.01, 0, 1, 1],
        [0, 0, 1.01, 1],
        [0, 0, float("nan"), 1],
        [0, 0, 1],
    ],
)
def test_invalid_geometry_is_rejected(box):
    with pytest.raises(ValueError):
        engine.checked_box(box)


def test_yolo_truth_and_native_index_mapping_preserve_canonical_coordinates():
    text = "0 0.25000000 0.50000000 0.50000000 1.00000000\n"
    assert engine.truth_from_yolo(text, 2) == truth((0, HALF))
    items = [
        {
            "type": "rectanglelabels",
            "native_class_index": 1,
            "score": 0.8,
            "value": {"x": 0, "y": 0, "width": 50, "height": 100},
        }
    ]
    mapped = engine.predictions_from_wire(items, [1, 0])
    assert mapped[0]["class_index"] == 0 and mapped[0]["native_class_index"] == 1
    assert mapped[0]["xyxy"] == list(HALF)
    with pytest.raises(ValueError):
        engine.predictions_from_wire(items * 101, [1, 0])
    with pytest.raises(ValueError):
        engine.truth_from_yolo("2 0.5 0.5 1 1", 2)


@pytest.mark.parametrize("orientation", [1, 6, 8])
def test_non_square_exif_is_transposed_once_and_normalized_bytes_have_no_metadata(
    orientation,
):
    original = Image.new("RGB", (4, 2))
    original.putdata([(255, 0, 0)] * 4 + [(0, 0, 255)] * 4)
    exif = Image.Exif()
    exif[274] = orientation
    exif[270] = "private fixture metadata"
    buffer = io.BytesIO()
    original.save(buffer, format="PNG", exif=exif)
    raw = buffer.getvalue()
    expected = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    normalized, identity = engine.normalize_image(
        raw, width=expected.width, height=expected.height
    )
    with Image.open(io.BytesIO(normalized)) as result:
        assert result.size == expected.size
        assert result.tobytes() == expected.tobytes()
        assert not result.getexif() and not result.info
    assert identity["raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert identity["derived_sha256"] == hashlib.sha256(normalized).hexdigest()
    assert identity["exif_orientation"] == orientation
    assert identity["raw_sha256"] != identity["derived_sha256"]
    again, _ = engine.normalize_image(
        normalized, width=expected.width, height=expected.height
    )
    assert again == normalized
    if orientation != 1:
        with pytest.raises(ValueError, match="logical dimensions"):
            engine.normalize_image(raw, width=4, height=2)


def test_normalization_enforces_byte_pixel_output_and_dimension_budgets(monkeypatch):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 4)).save(buffer, format="PNG")
    raw = buffer.getvalue()
    with pytest.raises(ValueError, match="logical dimensions"):
        engine.normalize_image(raw, width=4, height=8)
    monkeypatch.setattr(engine, "MAX_PIXELS", 31)
    with pytest.raises(ValueError, match="pixel/frame"):
        engine.normalize_image(raw, width=8, height=4)
    monkeypatch.setattr(engine, "MAX_PIXELS", 32)
    engine.normalize_image(raw, width=8, height=4)
    monkeypatch.setattr(engine, "MAX_RAW_BYTES", len(raw) - 1)
    with pytest.raises(ValueError, match="byte budget"):
        engine.normalize_image(raw, width=8, height=4)
    monkeypatch.setattr(engine, "MAX_RAW_BYTES", len(raw))
    monkeypatch.setattr(engine, "MAX_PNG_BYTES", 20)
    with pytest.raises(ValueError, match="byte budget"):
        engine.normalize_image(raw, width=8, height=4)


def test_benchmark_is_a_multiset_and_comparison_excludes_candidate_weight_and_provenance():
    member = {
        "task_id": "one",
        "sha256": "a" * 64,
        "label_sha256": "b" * 64,
        "width": 8,
        "height": 4,
    }
    key = engine.benchmark_identity([member], ["car"])
    assert key == engine.benchmark_identity([{**member, "task_id": "another"}], ["car"])
    assert key != engine.benchmark_identity([member, member], ["car"])
    spec = {
        "benchmark_digest": key,
        "classes": ["car"],
        "role": "validation_selection",
        "evaluator": engine.EVALUATOR,
        "preprocessing": engine.PREPROCESSING,
        "profile_digest": "a" * 64,
        "profile": {
            "storage_identity": str(uuid.uuid4()),
            "profile_digest": "a" * 64,
            "effective_device": "cpu",
            "options": {"imgsz": 640},
        },
        "confidence": 0.25,
        "matching_iou": 0.5,
        "nms_iou": 0.7,
        "model_ref": {"sha256": "c" * 64},
    }
    other = {**spec, "model_ref": {"sha256": "d" * 64, "provenance": "different"}}
    assert engine.comparison_identity(spec) == engine.comparison_identity(other)
    assert engine.comparison_identity(spec) != engine.comparison_identity(
        {**other, "matching_iou": 0.75}
    )
    assert engine.comparison_identity(spec) == engine.comparison_identity(
        {
            **other,
            "profile": {
                **spec["profile"],
                "storage_identity": str(uuid.uuid4()),
                "profile_digest": "e" * 64,
            },
        }
    )
    assert engine.comparison_identity(spec) != engine.comparison_identity(
        {**other, "profile": {**spec["profile"], "effective_device": "cuda:0"}}
    )


def test_huge_json_integer_is_a_validation_error():
    with pytest.raises(ValueError, match="finite"):
        engine.checked_box([0, 0, 10**400, 1])


def test_creation_requires_explicit_test_declaration_unique_members_and_real_thresholds():
    body = {
        "request_id": str(uuid.uuid4()),
        "name": " Evaluation ",
        "model_version_id": str(uuid.uuid4()),
        "deployment_id": str(uuid.uuid4()),
        "dataset_id": str(uuid.uuid4()),
        "sample_task_ids": [str(uuid.uuid4())],
    }
    assert SampleEvaluationCreate.model_validate(body).name == "Evaluation"
    for changed in (
        {"role": "held_out_test"},
        {"sample_task_ids": body["sample_task_ids"] * 2},
        {"confidence": True},
        {"matching_iou": float("nan")},
        {"name": " "},
    ):
        with pytest.raises(ValidationError):
            SampleEvaluationCreate.model_validate({**body, **changed})


@pytest.mark.parametrize("diagnostic", ["confusion", "localization"])
def test_maximum_evidence_shape_fits_verified_report_byte_and_node_budgets(diagnostic):
    from tests.sample_evaluation_fixtures import profile

    runtime = profile()
    classes = [f"类别{i:04d}" + "长" * 114 for i in range(1000)]
    samples = []
    for ordinal in range(32):
        gt = truth(*[(ordinal, FULL) for _ in range(63 if ordinal < 16 else 62)])
        pred = predictions(
            *[
                (ordinal + 500, 0.5, FULL)
                if diagnostic == "confusion"
                else (ordinal, 0.5, (0.0, 0.0, 0.25, 1.0))
                for _ in range(100)
            ]
        )
        result = {
            "status": "succeeded",
            "truth": gt,
            "predictions": pred,
            "matching": engine.evaluate_sample(
                gt, pred, class_count=len(classes), confidence=0.25, matching_iou=0.5
            ),
        }
        request_id = str(uuid.uuid4())
        result.update(
            ordinal=ordinal,
            task_id=str(uuid.uuid4()),
            sha256="a" * 64,
            label_sha256="b" * 64,
            width=640,
            height=640,
            derived_sha256="c" * 64,
            preprocessing={
                "raw_sha256": "a" * 64,
                "derived_sha256": "c" * 64,
                "width": 640,
                "height": 640,
                "exif_orientation": 8,
                "preprocessing_digest": content_digest(engine.PREPROCESSING),
                "size_bytes": 32 * 1024 * 1024,
            },
            request_id=request_id,
            binding_digest="d" * 64,
            inference={
                "request_id": request_id,
                "binding_digest": "d" * 64,
                "profile_digest": runtime["profile_digest"],
                "storage_identity": runtime["storage_identity"],
                "input_sha256": "c" * 64,
                "issued_at": 1792074000,
                "expires_at": 1792160400,
                "width": 640,
                "height": 640,
                "effective_device": "cpu",
                "options": runtime["options"],
                "conf": 0.25,
                "nms_iou": 0.7,
                "end2end": False,
            },
            produced_attempt_id=str(uuid.uuid4()),
            error_code=None,
            error_message=None,
        )
        result["evidence_digest"] = content_digest(result)
        samples.append(result)
    report = {
        "schema": 1,
        "evaluation_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
        "spec_digest": "a" * 64,
        "benchmark_digest": "b" * 64,
        "comparison_digest": "c" * 64,
        "model_ref": {
            "sha256": "d" * 64,
            "classes": classes,
            "bindings": classes,
            "provenance": {"runtime": runtime},
        },
        "evaluator": engine.EVALUATOR,
        "status": "succeeded",
        "complete": True,
        "total_samples": 32,
        "succeeded_samples": 32,
        "failed_samples": 0,
        "pending_samples": 0,
        "aggregate": engine.aggregate(samples, classes),
        "samples": samples,
    }
    parsed = EvaluationReport.model_validate(report)
    assert sum(len(s["truth"]) for s in samples) == 2000
    assert report["aggregate"]["diagnostics"][diagnostic] == 2000
    assert report["aggregate"]["fp"] == 3200 and report["aggregate"]["fn"] == 2000
    raw = json.dumps(
        parsed.model_dump(mode="json", by_alias=True), separators=(",", ":")
    ).encode()
    assert len(raw) < MAX_REPORT_BYTES
    _bounded(json.loads(raw), budget=[engine.REPORT_NODE_BUDGET])
    with pytest.raises(ValidationError):
        EvaluationReport.model_validate({**report, "complete": False})
    with pytest.raises(ValidationError):
        EvaluationReport.model_validate(
            {**report, "status": "failed", "complete": False}
        )
