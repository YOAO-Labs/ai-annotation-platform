"""Small original-pixel examples and immutable mask report verification."""

import copy
import hashlib
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aap_protocol_v2.mask_interaction import CocoRlePayload, canonical_rle_bytes
from fastapi import HTTPException
from training_contract.model_return import (
    SEGMENTATION_OUTPUT_CONTRACT,
    SEGMENTATION_RUNTIME,
)

from app.schemas.sample_evaluation import EvaluationSpec, SampleEvaluationCreate
from app.services import sample_evaluation_masks as masks, sample_evaluations as service
from app.services.training_dataset_bridge import content_digest
from training_contract.dataset import SEGMENTATION_CONTRACT, segmentation_labels


def rle(*counts, size=(4, 4)):
    return {"encoding": "coco_rle", "size": list(size), "counts": list(counts)}


FULL, HALF = rle(0, 16), rle(0, 8, 8)


def truth(*items):
    return [
        {"index": i, "class_index": cls, **masks.mask_evidence(mask)}
        for i, (cls, mask) in enumerate(items)
    ]


def predictions(*items):
    return [
        {
            "index": i,
            "class_index": cls,
            "native_class_index": cls,
            "score": score,
            **masks.mask_evidence(mask),
        }
        for i, (cls, score, mask) in enumerate(items)
    ]


def evaluate(gt, pred):
    return masks.evaluate_sample(
        gt, pred, class_count=2, confidence=0.25, matching_iou=0.5
    )


def wire(mask=FULL, native=0):
    payload = CocoRlePayload.model_validate(mask)
    raw = canonical_rle_bytes(payload)
    sha = hashlib.sha256(raw).hexdigest()
    return {
        "result": [
            {
                "tool_unit_id": "region",
                "native_class_index": native,
                "instance_index": 0,
                "score": 0.8,
                "confidence": 0.8,
                "geometry": {
                    "type": "raster_mask",
                    "mask": {
                        "encoding": "coco_rle_ref",
                        "size": mask["size"],
                        "sha256": sha,
                        "bytes": len(raw),
                        "runs": len(mask["counts"]),
                        "object_key": f"raster-masks/sha256/{sha[:2]}/{sha[2:4]}/{sha}.json",
                    },
                },
            }
        ],
        "meta": {
            "segmentation": {
                "dimensions": mask["size"],
                "output_contract": SEGMENTATION_OUTPUT_CONTRACT,
                "runtime_qualification": SEGMENTATION_RUNTIME,
                "decoder": SEGMENTATION_RUNTIME["decoder"],
                "mask_processing": SEGMENTATION_RUNTIME["mask_processing"],
            },
            "mask_objects": {sha: mask},
        },
    }


def test_exact_pixels_holes_disconnected_components_and_dice_are_not_bbox_overlap():
    a, b = rle(0, 1, 14, 1), rle(3, 1, 8, 1, 3)
    assert masks.mask_iou(a, b) == 0
    result = evaluate(truth((0, FULL)), predictions((0, 0.25, HALF)))
    assert result["matches"] == [
        {"prediction_index": 0, "truth_index": 0, "iou": 0.5, "dice": 2 / 3}
    ]
    ring = rle(0, 5, 2, 2, 2, 5)
    assert masks.mask_evidence(ring)["area_pixels"] == 12
    assert masks.mask_iou(ring, FULL) == 0.75


def test_semantic_identity_ignores_redundant_zero_runs_without_dense_allocation():
    assert masks.mask_evidence(rle(0, 3, 0, 5, 8)) == masks.mask_evidence(HALF)
    assert masks.mask_iou(HALF, HALF) == 1
    with pytest.raises(ValueError, match="dimensions"):
        masks.mask_iou(FULL, rle(0, 16, size=(2, 8)))


def test_stable_greedy_ties_duplicate_predictions_and_wrong_class_remain_fp_fn():
    result = evaluate(
        truth((0, FULL), (0, FULL)),
        predictions((0, 0.8, FULL), (0, 0.8, FULL), (0, 0.7, FULL)),
    )
    assert [(p["prediction_index"], p["truth_index"]) for p in result["matches"]] == [
        (0, 0),
        (1, 1),
    ]
    assert result["false_positive_indices"] == [2]
    result = evaluate(truth((0, FULL)), predictions((1, 0.8, FULL), (1, 0.7, FULL)))
    assert (result["tp"], result["fp"], result["fn"]) == (0, 2, 1)
    assert result["diagnostics"] == [
        {
            "kind": "confusion",
            "prediction_index": 0,
            "truth_index": 0,
            "iou": 1.0,
            "dice": 1.0,
        }
    ]
    assert evaluate([], [])["f1"] is None


def test_pixel_area_groups_and_matched_means_use_instance_area():
    gt, pred = truth((0, HALF)), predictions((0, 0.8, HALF), (1, 0.7, FULL))
    summary = masks.aggregate(
        [
            {
                "status": "succeeded",
                "truth": gt,
                "predictions": pred,
                "matching": evaluate(gt, pred),
            }
        ],
        ["car", "person"],
    )
    assert summary["matched_mean_iou"] == summary["matched_mean_dice"] == 1
    assert summary["area_groups"][0] == {
        "group": "small",
        "tp": 1,
        "fp": 1,
        "fn": 0,
        "precision": 0.5,
        "recall": 1.0,
        "f1": 2 / 3,
    }
    assert masks._area_group(1024) == "medium" and masks._area_group(9216) == "large"


@pytest.mark.parametrize(
    "mutation", ["sha", "bytes", "alignment", "native", "unused", "dimensions"]
)
def test_mask_wire_requires_exact_original_reference_and_native_alignment(mutation):
    raw = wire()
    if mutation == "unused":
        raw["meta"]["mask_objects"]["e" * 64] = HALF
    elif mutation == "dimensions":
        raw["meta"]["segmentation"]["dimensions"] = [2, 8]
    elif mutation == "alignment":
        raw["result"][0]["instance_index"] = 1
    elif mutation == "native":
        raw["result"][0]["native_class_index"] = True
    else:
        raw["result"][0]["geometry"]["mask"][
            "sha256" if mutation == "sha" else "bytes"
        ] = "f" * 64 if mutation == "sha" else 1
    with pytest.raises((ValueError, TypeError)):
        masks.normalize_predictions(raw, [1, 0], width=4, height=4)


def test_wire_mapping_and_work_budgets_fail_closed(monkeypatch):
    assert (
        masks.normalize_predictions(wire(), [1, 0], width=4, height=4)[0]["class_index"]
        == 1
    )
    monkeypatch.setattr(masks, "MAX_MATCH_RUN_OPERATIONS", 1)
    with pytest.raises(ValueError, match="budget"):
        evaluate(truth((0, FULL)), predictions((0, 0.8, FULL)))
    monkeypatch.setattr(masks, "MAX_SAMPLE_RLE_BYTES", 1)
    with pytest.raises(ValueError, match="budget"):
        masks.normalize_predictions(wire(), [0], width=4, height=4)


async def world(monkeypatch, *, raster=False):
    ids = {
        name: uuid.uuid4()
        for name in (
            "project",
            "model",
            "deployment",
            "dataset",
            "evaluation",
            "task",
            "annotation",
        )
    }
    annotation = {
        "id": str(ids["annotation"]),
        "version": 1,
        "class_name": "car",
        "tool_unit_id": "region",
        "geometry": {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    }
    text, _ = segmentation_labels([annotation], ["car", "person"])
    member = {
        "task_id": str(ids["task"]),
        "sha256": "1" * 64,
        "label_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "width": 4,
        "height": 4,
        "size_bytes": 100,
        "split": "val",
    }
    snapshot = {
        **SEGMENTATION_CONTRACT,
        "classes": ["car", "person"],
        "samples": [{"task_id": str(ids["task"]), "annotations": [annotation]}],
    }
    manifest = {
        **SEGMENTATION_CONTRACT,
        "classes": ["car", "person"],
        "samples": [member],
    }
    if raster:
        from app.services import training_dataset_analysis

        instance = {
            "annotation_id": str(ids["annotation"]),
            "annotation_version": 1,
            "class_index": 0,
            "pixel_sha256": "b" * 64,
            "rle_bytes": 70,
        }
        manifest.update(schema=4)
        member["instances"] = [instance]
        reader = Mock(
            return_value={instance["annotation_id"]: {**instance, "rle": FULL}}
        )
        monkeypatch.setattr(
            training_dataset_analysis, "read_instance_masks", reader, raising=False
        )
        monkeypatch.setattr(
            service, "checked_dataset", lambda version, task: version.manifest
        )
    manifest["content_digest"] = content_digest(manifest)
    version = SimpleNamespace(
        id=ids["dataset"],
        project_id=ids["project"],
        status="ready",
        snapshot=snapshot,
        snapshot_digest=content_digest(snapshot),
        manifest=manifest,
        package_sha256="2" * 64,
    )
    from tests.test_training_preview_storage_flow import reference
    from tests.training_consumer_fixtures import mock_legacy_transport

    version.storage_ref = reference(
        version.project_id, version.id, purpose="training-dataset"
    )
    mock_legacy_transport(monkeypatch)
    ref = {
        "task": "segment",
        "model_version_id": str(ids["model"]),
        "deployment_id": str(ids["deployment"]),
        "project_id": str(ids["project"]),
        "sha256": "3" * 64,
        "classes_digest": content_digest(["person", "car"]),
        "classes": ["person", "car"],
        "bindings": ["person", "car"],
    }
    ref["reference_digest"] = content_digest(ref)
    profile = {
        "schema": 1,
        "implementation": "aap-ultralytics-instance-mask",
        "revision": 1,
        "ultralytics_version": "8.4.177",
        "torch_version": "2.14.1+cu132",
        "pillow_version": "12.2.0",
        "source_digest": "4" * 64,
        "storage_identity": str(uuid.uuid4()),
        "effective_device": "cpu",
        "options": {"retina_masks": True},
    }
    profile["profile_digest"] = content_digest(profile)
    body = SampleEvaluationCreate(
        request_id=uuid.uuid4(),
        name="mask",
        model_version_id=ids["model"],
        deployment_id=ids["deployment"],
        dataset_id=ids["dataset"],
        sample_task_ids=[ids["task"]],
    )
    spec = await service.frozen_spec(
        SimpleNamespace(get=AsyncMock(return_value=version)),
        SimpleNamespace(id=ids["project"]),
        body,
        ids["evaluation"],
        ref,
        profile,
    )
    run = SimpleNamespace(
        id=ids["evaluation"],
        project_id=ids["project"],
        dataset_id=ids["dataset"],
        model_version_id=ids["model"],
        deployment_id=ids["deployment"],
        spec=spec,
        spec_digest=content_digest(spec),
    )
    return run, annotation


@pytest.mark.parametrize("raster", [False, True])
async def test_frozen_polygon_and_retained_raster_build_distinct_mask_spec(
    monkeypatch, raster
):
    run, annotation = await world(monkeypatch, raster=raster)
    spec = service.validate_spec(run)
    assert spec["schema"] == 2 and spec["samples"][0]["truth"][0]["rle"] == FULL
    assert spec["samples"][0]["truth"][0]["annotation_id"] == annotation["id"]
    assert "not_ap" in spec["evaluator"]["metrics"]
    altered = copy.deepcopy(spec)
    altered["schema"] = 1
    with pytest.raises(ValueError):
        EvaluationSpec.model_validate(altered)


async def test_shared_qualification_uses_independent_segment_profile_and_task_dispatch(
    monkeypatch,
):
    from app.services import managed_inference as managed

    run, _ = await world(monkeypatch)
    ref = {
        **run.spec["model_ref"],
        "registry_id": str(uuid.uuid4()),
        "framework": "ultralytics",
        "framework_version": "8.4.177",
    }
    monkeypatch.setattr(
        managed.trained_models, "resolve_reference", AsyncMock(return_value=ref)
    )
    monkeypatch.setattr(
        managed.trained_models,
        "enabled_backend",
        AsyncMock(return_value=SimpleNamespace()),
    )
    remote = AsyncMock(return_value=run.spec["profile"])
    monkeypatch.setattr(
        managed,
        "MLBackendClient",
        lambda _: SimpleNamespace(managed_evaluation_request=remote),
    )
    db = SimpleNamespace(
        get=AsyncMock(
            return_value=SimpleNamespace(
                model_version_id=run.model_version_id,
                registry_id=uuid.UUID(ref["registry_id"]),
            )
        )
    )
    checked_ref, profile = await managed.qualification(
        db, SimpleNamespace(id=run.project_id), run.model_version_id, run.deployment_id
    )
    assert checked_ref == ref and profile == run.spec["profile"]
    remote.assert_awaited_once_with("segment/profile")
    assert managed.segmentation_spec({"schema": 1, "model_ref": ref})
    assert not managed.segmentation_spec({"schema": 2, "model_ref": {"task": "detect"}})


def test_current_source_semantic_labels_compare_pixels_for_polygon_and_raster(
    monkeypatch,
):
    from app.services import sample_evaluation_sources as sources, raster_mask_storage

    annotation = {
        "tool_unit_id": "region",
        "class_name": "car",
        "geometry": {"type": "polygon", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
    }
    snapshot = {"annotations": [annotation], "expected_width": 4, "expected_height": 4}
    expected = content_digest([(0, masks.mask_evidence(FULL)["mask_sha256"])])
    assert sources.classify_labels(snapshot, ["car"], task="segment") == expected
    annotation["geometry"] = {"type": "raster_mask", "mask": {}}
    monkeypatch.setattr(raster_mask_storage, "load_coco_rle_sync", lambda _: FULL)
    assert sources.classify_labels(snapshot, ["car"], task="segment") == expected
    assert sources.classify_labels(snapshot, ["person"], task="segment") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("area_pixels", 2),
        ("mask_sha256", "a" * 64),
        ("native_class_index", 3),
        ("score", True),
    ],
)
def test_persisted_mask_predictions_recheck_pixels_and_native_mapping(field, value):
    evidence = predictions((0, 0.8, FULL))
    evidence[0][field] = value
    with pytest.raises(ValueError):
        masks.checked_predictions(evidence, class_count=2, native_to_benchmark=[0, 1])


@pytest.mark.parametrize(
    "tamper", [None, "pixels", "dice", "aggregate", "decoder", "profile"]
)
async def test_mask_service_execution_report_and_strict_comparison(monkeypatch, tamper):
    run, _ = await world(monkeypatch)
    frozen, spec = run.spec["samples"][0], run.spec
    attempt = SimpleNamespace(id=uuid.uuid4())
    row = SimpleNamespace(
        ordinal=0,
        request_id=uuid.uuid4(),
        status="succeeded",
        produced_attempt_id=attempt.id,
    )
    preprocessing = {
        "raw_sha256": frozen["sha256"],
        "derived_sha256": "5" * 64,
        "preprocessing_digest": content_digest(masks.PREPROCESSING),
        "width": 4,
        "height": 4,
    }
    row.request = service.request_identity(spec, row, preprocessing)
    raw = wire(native=1)
    raw["meta"].update(
        trained_model={
            key: spec["model_ref"][key]
            for key in ("model_version_id", "sha256", "classes_digest")
        },
        sample_evaluation={
            **row.request["evaluation"],
            "width": 4,
            "height": 4,
            "effective_device": "cpu",
            "options": spec["profile"]["options"],
            "conf": spec["confidence"],
            "nms_iou": spec["nms_iou"],
            "end2end": False,
        },
    )
    row.evidence = service.successful_evidence(
        run,
        attempt,
        row,
        SimpleNamespace(
            task_id=frozen["task_id"], result=raw["result"], meta=raw["meta"]
        ),
    )
    report = service.build_report(run, attempt, [row], "succeeded")
    assert (
        report["aggregate"]["tp"] == 1 and report["aggregate"]["matched_mean_dice"] == 1
    )
    if tamper is None:
        return
    if tamper == "aggregate":
        report["aggregate"]["matched_mean_dice"] = 0.2
    elif tamper in {"decoder", "profile"}:
        run.spec["evaluator" if tamper == "decoder" else "profile"][
            "decoder" if tamper == "decoder" else "source_digest"
        ] = "changed"
        run.spec_digest = content_digest(run.spec)
    else:
        item = report["samples"][0]
        if tamper == "pixels":
            item["predictions"][0]["rle"] = HALF
        else:
            item["matching"]["matches"][0]["dice"] = 0.2
        item["evidence_digest"] = content_digest(
            {k: v for k, v in item.items() if k != "evidence_digest"}
        )
        row.evidence = copy.deepcopy(item)
    with pytest.raises((ValueError, HTTPException)):
        service.verify_report(report, run, attempt, [row])


async def test_frozen_rle_truth_reads_the_sample_once_and_rejects_oversize_before_io(
    monkeypatch,
):
    from app.services import training_dataset_analysis
    from training_contract.dataset import instance_rle_evidence

    task_id = uuid.uuid4()
    instances = [
        {
            "annotation_id": str(uuid.uuid4()),
            "annotation_version": 1,
            "class_index": 0,
            **instance_rle_evidence(mask),
        }
        for mask in (FULL, HALF)
    ]
    retained = {
        item["annotation_id"]: {**item, "rle": mask}
        for item, mask in zip(instances, (FULL, HALF), strict=True)
    }
    reader = Mock(return_value=retained)
    monkeypatch.setattr(training_dataset_analysis, "read_instance_masks", reader)
    member = {
        "task_id": str(task_id),
        "label_sha256": "a" * 64,
        "width": 4,
        "height": 4,
        "instances": instances,
    }
    version = SimpleNamespace(manifest={"schema": 4})
    from app.services import training_dataset_storage, training_storage_io

    monkeypatch.setattr(training_dataset_storage, "resolve", lambda version: object())

    async def retained_read(db, owner, purpose, callback):
        return callback(object())

    monkeypatch.setattr(training_storage_io, "run", retained_read)
    result = await service.frozen_mask_truth(
        uuid.uuid4(), version, member, {}, ["shape"], db=None
    )
    assert len(result) == 2 and reader.call_count == 1
    reader.reset_mock()
    instances[0]["rle_bytes"] = masks.MAX_SAMPLE_RLE_BYTES + 1
    with pytest.raises(ValueError, match="RLE budget"):
        await service.frozen_mask_truth(
            uuid.uuid4(), version, member, {}, ["shape"], db=None
        )
    reader.assert_not_called()
