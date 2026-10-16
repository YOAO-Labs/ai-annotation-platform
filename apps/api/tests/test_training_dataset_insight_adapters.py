"""Task qualifications and frozen aggregate denominators are independent of UI."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import uuid

import pytest
from pydantic import TypeAdapter, ValidationError
from training_contract.dataset import (
    DETECTION_CONTRACT,
    SEGMENTATION_CONTRACT,
    segmentation_labels,
)

from app.schemas.training_dataset_analysis import TrainingDatasetTaskStatistics
from app.services import training_dataset_analysis as analysis
from app.services import training_dataset_insight_adapters as adapters
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from tests.test_training_dataset_analysis import inputs, sha


def derive(specs, *, task="detect", **kwargs):
    snapshot, manifest = inputs(specs, **kwargs)
    if task == "segment":
        snapshot.update(SEGMENTATION_CONTRACT)
        manifest.update(SEGMENTATION_CONTRACT)
        for source, member in zip(
            snapshot["samples"], manifest["samples"], strict=True
        ):
            annotations = [
                {
                    "id": str(uuid.uuid5(uuid.UUID(source["task_id"]), str(index))),
                    "version": 1,
                    "class_name": annotation["class_name"],
                    "tool_unit_id": "region",
                    "geometry": {
                        "type": "polygon",
                        "points": [
                            [0.1, 0.2],
                            [0.1 + annotation["geometry"]["w"], 0.2],
                            [0.1 + annotation["geometry"]["w"], 0.6],
                            [0.1, 0.6],
                        ],
                    },
                }
                for index, annotation in enumerate(source["annotations"])
            ]
            source["annotations"] = annotations
            text, instances = segmentation_labels(annotations, snapshot["classes"])
            member.update(
                label_sha256=hashlib.sha256(text.encode()).hexdigest(),
                instances=instances,
            )
    return analysis.derive(snapshot, manifest, None, content_digest(snapshot))


@pytest.mark.parametrize(
    "record, expected",
    [
        ({"schema": 1}, "image-detection-bbox"),
        (dict(DETECTION_CONTRACT), "image-detection-bbox"),
        (
            {
                "schema": 2,
                "task": "image-instance-segmentation",
                "geometry": "normalized-simple-polygon",
                "bridge": "ultralytics-simple-polygon-1",
            },
            "image-segmentation-simple-polygon",
        ),
        (dict(SEGMENTATION_CONTRACT), "image-segmentation-simple-polygon"),
    ],
)
def test_accepted_historical_profiles_resolve_without_mutating_records(
    record, expected
):
    original = deepcopy(record)
    adapter = adapters.adapter_for(record)
    assert adapter.id == expected
    assert record == original
    assert adapter.revision > 0
    assert all(entry["supported"] for entry in adapter.capabilities().values())


@pytest.mark.parametrize(
    "record",
    [
        {**DETECTION_CONTRACT, "media_type": "pointcloud"},
        {**DETECTION_CONTRACT, "geometry": "3d-box"},
        {**DETECTION_CONTRACT, "bridge": "other-bridge"},
        {**DETECTION_CONTRACT, "task": "pose"},
        {"schema": 1, "media_type": "video"},
    ],
)
def test_task_alone_never_qualifies_an_adapter(record):
    with pytest.raises(TrainingDatasetError):
        adapters.adapter_for(record)


def test_nonimage_source_does_not_gain_creation_or_training_qualification():
    adapter = adapters.adapter_for(DETECTION_CONTRACT)
    capabilities = adapter.capabilities({"source": {"media_type": "video"}})
    assert not capabilities["create_version"]["supported"]
    assert not capabilities["qualified_training"]["supported"]
    assert capabilities["task_statistics"]["supported"]
    assert capabilities["sample_preview"]["supported"]


def test_pixel_axes_have_six_exact_bins_and_count_training_members_not_contents():
    sizes = [255, 256, 511, 512, 1023, 1024, 2047, 2048, 4095, 4096]
    value = derive(
        [
            {"content": "same", "task": str(i), "width": size, "height": size}
            for i, size in enumerate(sizes)
        ]
    )
    statistics = value["overview"]["task_statistics"]
    dimensions = statistics["image_dimensions"]
    assert value["overview"]["counts"]["unique_contents"] == 1
    assert dimensions["unit"] == "member_images" and dimensions["population"] == 10
    assert dimensions["unknown_members"] == 0
    assert [dimensions["counts"][i][i] for i in range(6)] == [1, 2, 2, 2, 2, 1]
    assert sum(map(sum, dimensions["counts"])) == 10
    assert dimensions["x_bins"][1]["lower"] == 256
    assert dimensions["x_bins"][-1]["upper"] is None
    assert (
        TypeAdapter(TrainingDatasetTaskStatistics).validate_python(statistics).task
        == "detect"
    )


def test_unknown_members_are_not_negatives_density_zeroes_or_absent_classes():
    snapshot, manifest = inputs(
        [
            {"content": "known", "labels": ["car", "car"]},
            {"content": "negative", "labels": []},
            {"content": "missing", "labels": ["truck"]},
        ]
    )
    snapshot["samples"][2].pop("annotations")
    manifest["samples"][2].pop("width")
    value = analysis.derive(snapshot, manifest, None, content_digest(snapshot))
    overview = value["overview"]
    assert overview["status"] == "partial"
    assert overview["evidence"] == {
        "analyzed_members": 2,
        "unknown_label_members": 1,
        "known_instances": 2,
        "geometry_unknown_members": 1,
    }
    assert overview["counts"]["negatives"] == 1
    assert overview["observations"]["observed_classes"] == 1
    assert "classes_absent_from_train" not in overview["observations"]
    density = overview["task_statistics"]["objects_per_member"]
    assert density["analyzed_members"] == 2 and density["unknown_label_members"] == 1
    assert [row["count"] for row in density["bins"]] == [1, 0, 1, 0, 0, 0]
    assert overview["task_statistics"]["box_centers"]["population"] == 2
    assert overview["task_statistics"]["box_centers"]["unknown_members"] == 1
    assert overview["task_statistics"]["image_dimensions"]["population"] == 2
    assert overview["task_statistics"]["image_dimensions"]["unknown_members"] == 1


def test_density_buckets_and_normalized_upper_edges_are_bounded():
    counts = [0, 1, 2, 5, 6, 10, 11, 20, 21]
    value = derive(
        [{"content": str(count), "labels": ["car"] * count} for count in counts]
    )
    statistics = value["overview"]["task_statistics"]
    assert [row["count"] for row in statistics["objects_per_member"]["bins"]] == [
        1,
        1,
        2,
        2,
        2,
        1,
    ]
    member = {
        "width": 4096,
        "height": 4096,
        "instances": 1,
        "labels": [{"x_center": 1, "y_center": 1, "width": 1, "height": 1}],
    }
    edge = adapters.detection_statistics([member])
    assert edge["box_centers"]["counts"][19][19] == 1
    assert edge["box_dimensions"]["counts"][19][19] == 1
    assert all(len(row) == 20 for row in edge["box_centers"]["counts"])


def test_native_rounding_collapse_is_geometry_unknown_without_changing_frozen_labels():
    # The historical bridge can round a tiny positive width to zero. Keep those
    # frozen bytes and known labels, but do not claim valid box geometry.
    value = derive([{"content": "tiny", "w": 1e-10}])
    overview = value["overview"]
    assert overview["status"] == "partial"
    assert overview["evidence"]["known_instances"] == 1
    assert overview["evidence"]["unknown_label_members"] == 0
    assert overview["evidence"]["geometry_unknown_members"] == 1
    assert overview["task_statistics"]["box_dimensions"]["population"] == 0
    assert overview["task_statistics"]["objects_per_member"]["analyzed_members"] == 1


def test_detection_legacy_and_canonical_comparison_is_semantic_and_directional():
    snapshot, manifest = inputs(
        [{"content": "same", "labels": ["truck", "car", "car"], "split": "val"}]
    )
    baseline = analysis.derive(snapshot, manifest, None, content_digest(snapshot))
    snapshot.update(DETECTION_CONTRACT)
    manifest.update(DETECTION_CONTRACT)
    target = analysis.derive(snapshot, manifest, None, content_digest(snapshot))
    result = analysis.compare(baseline, target)
    assert result["counts"]["labels_changed"] == 0
    assert result["counts"]["unchanged"] == 1
    assert result["count_unit"] == "unique_contents"
    assert result["baseline_members"] == result["target_members"] == 1
    assert result["overlapping_changes"] is True
    assert result["semantic_unchanged_label"] == "训练语义未变化"
    assert result["semantic_validation_changed"] is False
    assert result["evaluation_benchmark_changed"] is False
    assert result["raw_contract_changed"] is True
    before_validation = baseline["overview"]["validation"]
    after_validation = target["overview"]["validation"]
    assert before_validation["semantic_digest"] != after_validation["semantic_digest"]
    assert (
        before_validation["raw_contract_digest"]
        != after_validation["raw_contract_digest"]
    )
    assert (
        before_validation["comparison_identity"]
        == after_validation["comparison_identity"]
    )
    changed = derive([{"content": "same", "labels": ["truck", "car"], "split": "val"}])
    assert analysis.compare(baseline, changed)["counts"]["labels_changed"] == 1


@pytest.mark.parametrize(
    "replacement, classes, semantic_changed, evaluation_changed",
    [
        ([{"content": "changed", "split": "val"}], None, True, True),
        ([{"content": "validation", "w": 0.5, "split": "val"}], None, True, True),
        ([{"content": "validation", "split": "train"}], None, True, True),
        (
            [
                {"content": "validation", "split": "val"},
                {
                    "content": "validation",
                    "task": "duplicate-validation",
                    "split": "val",
                },
            ],
            None,
            False,
            True,
        ),
        ([{"content": "validation", "split": "val"}], ["truck", "car"], True, True),
        (
            [{"content": "validation", "task": "new-work-item", "split": "val"}],
            None,
            False,
            False,
        ),
    ],
)
@pytest.mark.parametrize("task", ["detect", "segment"])
def test_normalized_validation_keeps_member_label_split_encoding_and_multiplicity_changes(
    replacement, classes, semantic_changed, evaluation_changed, task
):
    baseline = derive(
        [{"content": "train"}, {"content": "validation", "split": "val"}], task=task
    )
    target = derive(
        [{"content": "train"}, *replacement],
        task=task,
        **({"classes": classes} if classes else {}),
    )
    compared = analysis.compare(baseline, target)
    assert compared["semantic_validation_changed"] is semantic_changed
    assert compared["evaluation_benchmark_changed"] is evaluation_changed
    assert compared["raw_contract_changed"] is False


def test_normalized_validation_reports_missing_identity_as_unknown():
    snapshot, manifest = inputs(
        [{"content": "missing", "split": "val", "sha256": None}]
    )
    value = analysis.derive(snapshot, manifest, None, content_digest(snapshot))
    identity = value["overview"]["validation"]["comparison_identity"]
    assert identity["semantic_digest"] is None
    assert identity["evaluation_multiset_digest"] is None
    compared = analysis.compare(value, value)
    assert compared["semantic_validation_changed"] is None
    assert compared["evaluation_benchmark_changed"] is None


def test_comparison_identity_schema_revision_invalidates_old_derived_cache(monkeypatch):
    snapshot, manifest = inputs([{"content": "a"}])
    before = analysis.analysis_input_digest(
        content_digest(snapshot), manifest, sha("package")
    )
    monkeypatch.setattr(analysis, "ANALYSIS_SCHEMA", 3)
    assert before != analysis.analysis_input_digest(
        content_digest(snapshot), manifest, sha("package")
    )


def test_adapter_revision_invalidates_only_derived_identity(monkeypatch):
    snapshot, manifest = inputs([{"content": "a"}])
    original = deepcopy((snapshot, manifest))
    frozen_digest = content_digest(snapshot)
    before = analysis.analysis_input_digest(frozen_digest, manifest, sha("package"))
    adapter = adapters.adapter_for(manifest)
    monkeypatch.setitem(
        adapters._ADAPTERS,
        adapter.profile,
        replace(adapter, revision=adapter.revision + 1),
    )
    after = analysis.analysis_input_digest(frozen_digest, manifest, sha("package"))
    assert before != after
    assert (snapshot, manifest) == original
    assert content_digest(snapshot) == frozen_digest


def test_unsupported_statistics_never_receive_detection_payloads():
    snapshot, manifest = inputs([{"content": "a"}])
    manifest.update({**DETECTION_CONTRACT, "geometry": "other-geometry"})
    value = analysis.derive(snapshot, manifest, None, content_digest(snapshot))
    assert value["overview"]["status"] == "unavailable"
    assert value["overview"]["task_statistics"] is None
    assert not value["overview"]["capabilities"]["semantic_comparison"]["supported"]
    assert value["samples"][0]["is_negative"] is None
    with pytest.raises(TrainingDatasetError, match="没有足够证据"):
        analysis.compare(value, value)
    with pytest.raises(ValidationError):
        TypeAdapter(TrainingDatasetTaskStatistics).validate_python(
            {"task": "pose", "schema_version": 1}
        )


def test_parent_accepts_legacy_output_and_existing_grouping_without_rewrite():
    snapshot, manifest = inputs([{"content": "a"}, {"content": "b", "split": "val"}])
    original = deepcopy((snapshot, manifest))
    adapters.assert_parent_compatible(snapshot, manifest, DETECTION_CONTRACT)
    assert (snapshot, manifest) == original


@pytest.mark.parametrize(
    "change",
    [
        {"source": {"media_type": "video"}},
        {"split_grouping": "video-content-scene-2"},
    ],
)
def test_parent_rejects_unqualified_source_grouping(change):
    snapshot, manifest = inputs([{"content": "a"}])
    snapshot.update(change)
    with pytest.raises(TrainingDatasetError) as error:
        adapters.assert_parent_compatible(snapshot, manifest, DETECTION_CONTRACT)
    assert error.value.code == "parent_grouping_mismatch"


def test_parent_checks_output_and_cumulative_grouping_conflicts():
    snapshot, manifest = inputs([{"content": "a"}])
    with pytest.raises(TrainingDatasetError) as error:
        adapters.assert_parent_compatible(snapshot, manifest, SEGMENTATION_CONTRACT)
    assert error.value.code == "task_mismatch"
    manifest["split_history"] = {"content:" + sha("a"): "val"}
    with pytest.raises(TrainingDatasetError, match="分组证据"):
        adapters.assert_parent_compatible(snapshot, manifest, DETECTION_CONTRACT)
    manifest["split_history"] = {"frame-grid:unknown": "train"}
    with pytest.raises(TrainingDatasetError) as error:
        adapters.assert_parent_compatible(snapshot, manifest, DETECTION_CONTRACT)
    assert error.value.code == "parent_grouping_mismatch"
