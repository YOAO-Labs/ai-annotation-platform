"""Frozen statistics, semantic comparisons, indexed reads and verified media."""

import hashlib
import io
import json
import uuid

import pytest
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import func, select

from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_dataset_analysis import (
    TrainingDatasetAnalysis,
    TrainingDatasetAnalyzedSample,
)
from app.services import training_dataset_analysis as service
from app.services.training_dataset_bridge import content_digest, detection_labels
from tests.factory import create_membership, create_project


def sha(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def inputs(specs, *, classes=None, history=None, deduplicate=False):
    classes = classes or ["car", "truck"]
    frozen, members = [], []
    for spec in specs:
        task_id = str(uuid.uuid5(uuid.NAMESPACE_URL, spec.get("task", spec["content"])))
        annotations = [
            {
                "class_name": name,
                "tool_unit_id": "bbox",
                "geometry": {
                    "type": "bbox",
                    "x": 0.1,
                    "y": 0.2,
                    "w": spec.get("w", 0.3),
                    "h": 0.4,
                },
            }
            for name in spec.get("labels", ["car"])
        ]
        frozen.append(
            {
                "task_id": task_id,
                "annotations": annotations,
                "eligibility": {"negative": not annotations},
                "source_key": "mutable-source.png",
                "source_bucket": "mutable-inputs",
            }
        )
        text = detection_labels(annotations, classes)
        members.append(
            {
                "task_id": task_id,
                "sha256": spec.get("sha256", sha(spec["content"])),
                "label_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "split": spec.get("split", "train"),
                "width": spec.get("width", 16),
                "height": spec.get("height", 12),
                "size_bytes": spec.get("size_bytes", 128),
                "image_path": f"images/{spec.get('split', 'train')}/{task_id}.png",
                "label_path": f"labels/{spec.get('split', 'train')}/{task_id}.txt",
            }
        )
    snapshot = {
        "schema": 1,
        "classes": classes,
        "samples": frozen,
        "seed": 42,
        "validation_fraction": 0.2,
    }
    if deduplicate:
        snapshot["deduplicate"] = True
    manifest = {
        "schema": 1,
        "classes": classes,
        "samples": members,
        "source_digest": content_digest(snapshot),
        "split_history": history or {},
    }
    manifest["validation_digest"] = content_digest(
        [m for m in members if m["split"] == "val"]
    )
    manifest["content_digest"] = content_digest(manifest)
    return snapshot, manifest


def derived(specs, **kwargs):
    snapshot, manifest = inputs(specs, **kwargs)
    return service.derive(snapshot, manifest, None, content_digest(snapshot))


def test_polygon_insights_use_instance_area_and_keep_source_identity():
    from training_contract.dataset import SEGMENTATION_CONTRACT, segmentation_labels

    snapshot, manifest = inputs([{"content": "polygon", "split": "val"}])
    snapshot.update(SEGMENTATION_CONTRACT)
    manifest.update(SEGMENTATION_CONTRACT)
    ring = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.15], [0.15, 0.15], [0.15, 0.9], [0.1, 0.9]]
    annotation = {
        "id": str(uuid.uuid4()),
        "version": 3,
        "tool_unit_id": "region",
        "class_name": "car",
        "geometry": {"type": "polygon", "points": ring},
    }
    snapshot["samples"][0]["annotations"] = [annotation]
    text, mapping = segmentation_labels([annotation], snapshot["classes"])
    manifest["samples"][0].update(
        instances=mapping, label_sha256=hashlib.sha256(text.encode()).hexdigest()
    )
    result = service.derive(snapshot, manifest, None, content_digest(snapshot))
    assert result["overview"]["status"] == "complete"
    assert result["overview"]["task"] == SEGMENTATION_CONTRACT["task"]
    label = result["samples"][0]["labels"][0]
    assert label["type"] == "polygon" and label["annotation_id"] == annotation["id"]
    assert label["annotation_version"] == 3
    assert label["area"] == pytest.approx(0.0775)
    assert result["overview"]["box_area_bins"][1]["val"] == 1
    assert result["overview"]["task_statistics"]["task"] == "segment"
    assert result["overview"]["task_statistics"]["polygon_area_sum"] == pytest.approx(
        0.0775
    )
    assert "box_centers" not in result["overview"]["task_statistics"]
    # Recognition modernizes only the derived adapter profile. Historical
    # polygon labels and their source receipt remain exactly the same inputs.
    from training_contract.dataset import LEGACY_SEGMENTATION_CONTRACT

    for record in (snapshot, manifest):
        record.pop("media_type")
        record.pop("format")
        record.update(LEGACY_SEGMENTATION_CONTRACT)
    legacy = service.derive(snapshot, manifest, None, content_digest(snapshot))
    compared = service.compare(legacy, result)
    assert compared["counts"]["labels_changed"] == 0
    assert compared["semantic_validation_changed"] is False
    assert compared["evaluation_benchmark_changed"] is False
    assert compared["raw_contract_changed"] is True
    assert compared["validation_comparison_basis"] == "canonical-profile-validation-1"
    before_validation = legacy["overview"]["validation"]
    after_validation = result["overview"]["validation"]
    assert before_validation["semantic_digest"] != after_validation["semantic_digest"]
    assert (
        before_validation["evaluation_multiset_digest"]
        != after_validation["evaluation_multiset_digest"]
    )
    assert (
        before_validation["comparison_identity"]
        == after_validation["comparison_identity"]
    )
    detection = derived([{"content": "polygon", "split": "val"}])
    with pytest.raises(ValueError, match="不同训练任务"):
        service.compare(detection, result)


def test_statistics_use_actual_package_members_and_separate_image_instance_counts():
    snapshot, manifest = inputs(
        [
            {"content": "a", "labels": ["car", "car", "truck"]},
            {"content": "a", "task": "duplicate-a", "labels": ["car", "car", "truck"]},
            {"content": "b", "labels": [], "split": "val"},
        ],
        deduplicate=True,
    )
    manifest["samples"].pop(1)
    value = service.derive(snapshot, manifest, None, content_digest(snapshot))
    overview = value["overview"]
    assert overview["counts"]["images"] == 2
    assert overview["counts"]["instances"] == 3
    assert overview["counts"]["explicit_negatives"] == 1
    assert overview["counts"]["frozen_unpacked_images"] == 1
    assert value["classes"][0]["train_images"] == 1
    assert value["classes"][0]["train_instances"] == 2
    assert sum(b["train"] for b in overview["box_area_bins"]) == 3
    assert (
        next(s for s in value["samples"] if not s["included"])["exclusion_reason"]
        == "package_deduplicated"
    )
    assert not overview["preparation"]["recorded"]


def test_class_order_and_task_churn_do_not_invent_label_corrections():
    original = [
        {"content": "a", "labels": ["car", "truck"]},
        {"content": "b", "split": "val"},
    ]
    baseline = derived(original)
    target = derived(
        [{**s, "task": "replacement-" + s["content"]} for s in original],
        classes=["truck", "car"],
    )
    compared = service.compare(baseline, target)
    assert compared["class_order_changed"] and compared["class_encoding_changed"]
    assert compared["counts"]["labels_changed"] == 0
    assert compared["counts"]["task_ids_changed"] == 2
    assert compared["counts"]["added"] == compared["counts"]["removed"] == 0
    assert compared["evaluation_benchmark_changed"] is True
    churn_only = derived(
        [{**s, "task": "replacement-" + s["content"]} for s in original]
    )
    compared = service.compare(baseline, churn_only)
    assert compared["evaluation_benchmark_changed"] is False
    assert compared["semantic_validation_changed"] is False
    assert (
        baseline["overview"]["validation"]["manifest_digest"]
        != churn_only["overview"]["validation"]["manifest_digest"]
    )


def test_validation_multiplicity_changes_evaluation_but_not_deduplicated_identity():
    original = [{"content": "a"}, {"content": "b", "split": "val"}]
    compared = service.compare(
        derived(original),
        derived(
            [
                *original,
                {"content": "b", "task": "second-validation-copy", "split": "val"},
            ]
        ),
    )
    assert compared["semantic_validation_changed"] is False
    assert compared["evaluation_benchmark_changed"] is True
    assert compared["counts"]["multiplicity_changed"] == 1


def test_directional_diff_covers_corrections_splits_ancestry_and_class_names():
    baseline = derived(
        [{"content": "a"}, {"content": "b", "split": "val"}, {"content": "removed"}],
        history={"content:" + sha("returning"): "train"},
    )
    target = derived(
        [
            {"content": "a", "w": 0.5},
            {"content": "b"},
            {"content": "returning", "labels": ["new-class"]},
        ],
        classes=["car", "new-class"],
    )
    result = service.compare(baseline, target)
    assert {
        k: result["counts"][k]
        for k in ("added", "removed", "labels_changed", "split_changed", "reintroduced")
    } == {
        "added": 1,
        "removed": 1,
        "labels_changed": 1,
        "split_changed": 1,
        "reintroduced": 1,
    }
    assert result["classes_added"] == result["classes_removed"] == 1
    assert not result["class_order_changed"]


def test_partial_metadata_is_explicit_and_never_reads_mutable_sources():
    snapshot, manifest = inputs([{"content": "a"}, {"content": "b", "split": "val"}])
    snapshot["samples"][0].pop("annotations")
    manifest["samples"][0].pop("width")
    result = service.derive(snapshot, manifest, None, content_digest(snapshot))
    assert result["overview"]["status"] == "partial"
    assert result["overview"]["counts"]["known_label_images"] == 1
    assert result["overview"]["dimensions"]["unknown"] == 1
    assert result["samples"][0]["instances"] is None
    assert result["samples"][0]["labels"] == []
    assert result["samples"][0]["issue"] == "frozen_labels_unavailable"


@pytest.mark.parametrize(
    "before, after, unknown, split_changed, unchanged",
    [
        (None, "val", 1, 0, 0),
        ("val", None, 1, 0, 0),
        (None, None, 1, 0, 0),
        ("train", "val", 0, 1, 0),
        ("val", "val", 0, 0, 1),
    ],
)
def test_unknown_split_evidence_never_proves_changed_or_unchanged(
    before, after, unknown, split_changed, unchanged
):
    baseline = derived([{"content": "same", "split": before}])
    target = derived([{"content": "same", "split": after}])
    assert baseline["contents"][sha("same")]["unknown_split_members"] == int(
        before is None
    )
    assert target["contents"][sha("same")]["unknown_split_members"] == int(
        after is None
    )
    compared = service.compare(baseline, target)
    assert compared["counts"]["unknown"] == unknown
    assert compared["counts"]["split_changed"] == split_changed
    assert compared["counts"]["unchanged"] == unchanged
    if unknown:
        assert compared["changes"]["unknown"] == {sha("same")}
        assert compared["semantic_validation_changed"] is None
        assert compared["evaluation_benchmark_changed"] is None


@pytest.mark.parametrize("width, labels_changed", [(0.3, 0), (0.5, 1)])
def test_mixed_known_unknown_split_members_keep_independent_confirmed_changes(
    width, labels_changed
):
    baseline = derived(
        [
            {"content": "same", "task": "known", "split": "train"},
            {"content": "same", "task": "unknown", "split": None},
        ]
    )
    target = derived([{"content": "same", "task": "known", "split": "val", "w": width}])
    assert baseline["contents"][sha("same")]["splits"] == ["train"]
    assert baseline["contents"][sha("same")]["unknown_split_members"] == 1
    compared = service.compare(baseline, target)
    assert compared["counts"]["unknown"] == 1
    assert compared["counts"]["split_changed"] == 0
    assert compared["counts"]["unchanged"] == 0
    assert compared["counts"]["labels_changed"] == labels_changed
    assert compared["counts"]["multiplicity_changed"] == 1


def test_missing_derived_split_evidence_is_unknown_until_cache_rebuild():
    baseline = derived([{"content": "same", "split": "val"}])
    target = derived([{"content": "same", "split": "val"}])
    baseline["contents"][sha("same")].pop("unknown_split_members")
    compared = service.compare(baseline, target)
    assert compared["counts"]["unknown"] == 1
    assert compared["counts"]["unchanged"] == 0


async def version(
    db, owner, *, project=None, specs=None, classes=None, preparation=False
):
    project = project or await create_project(db, owner_id=owner.id)
    snapshot, manifest = inputs(
        specs or [{"content": "a"}, {"content": "b", "split": "val"}], classes=classes
    )
    digest = content_digest(snapshot)
    report = (
        {
            "schema": 1,
            "snapshot_digest": digest,
            "summary": {
                "selected": len(snapshot["samples"]) + 1,
                "eligible": len(snapshot["samples"]),
                "positives": len(snapshot["samples"]),
                "negatives": 0,
                "excluded": 1,
            },
            "issues": [],
            "excluded": [{"task_id": str(uuid.uuid4()), "code": "not_reviewed"}],
        }
        if preparation
        else None
    )
    value = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        name="Frozen analysis",
        request_id=uuid.uuid4(),
        request_digest=digest,
        snapshot=snapshot,
        snapshot_digest=digest,
        manifest=manifest,
        preparation_report=report,
        status="ready",
        package_sha256="f" * 64,
    )
    db.add(value)
    await db.flush()
    from app.services.training_dataset_storage import allocate

    await allocate(db, value)
    return project, value


async def test_indexed_pages_filters_and_frozen_preparation_are_bounded(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    specs = [
        {
            "content": f"sample-{i}",
            "labels": [] if i % 3 == 0 else ["car"] if i % 3 == 1 else ["truck"],
            "split": "val" if i % 2 else "train",
        }
        for i in range(160)
    ]
    project, data = await version(db_session, owner, specs=specs, preparation=True)
    path = f"/api/v1/projects/{project.id}/training-datasets/{data.id}/insights"
    headers = {"Authorization": "Bearer " + token}
    response = await httpx_client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["images"] == 160
    assert response.json()["preparation"]["recorded"]
    assert response.json()["media"] == {
        "verified": 0,
        "unavailable": 0,
        "unchecked": 160,
        "last_checked_at": None,
    }
    assert len(response.content) < 30000
    assert response.json()["task_statistics"]["task"] == "detect"
    assert response.json()["evidence"]["analyzed_members"] == 160
    compact = await httpx_client.get(
        path.removesuffix("/insights") + "?include_manifest=false", headers=headers
    )
    assert compact.status_code == 200 and compact.json()["manifest"] == {}
    assert len(compact.content) < 2000
    monkeypatch.setattr(
        service,
        "derive",
        lambda *args: pytest.fail("A fresh cache must not rescan frozen annotations"),
    )
    page = await httpx_client.get(
        path + "/samples?kind=negative&split=val&limit=7&offset=3", headers=headers
    )
    assert page.status_code == 200, page.text
    expected = sorted(
        s["task_id"]
        for i, s in enumerate(data.manifest["samples"])
        if i % 3 == 0 and i % 2
    )
    assert page.json()["total"] == len(expected)
    assert [s["task_id"] for s in page.json()["items"]] == expected[3:10]
    assert all(
        "labels" not in s and "source_key" not in s for s in page.json()["items"]
    )
    class_page = await httpx_client.get(
        path + "/classes?search=truck&limit=1", headers=headers
    )
    assert (
        class_page.json()["total"] == 1
        and class_page.json()["items"][0]["name"] == "truck"
    )
    truck = await httpx_client.get(
        path + "/samples?class_index=1&limit=100", headers=headers
    )
    assert truck.json()["total"] == sum(i % 3 == 2 for i in range(160))
    excluded = await httpx_client.get(path + "/samples?kind=excluded", headers=headers)
    assert (
        excluded.json()["total"] == 1
        and excluded.json()["items"][0]["exclusion_reason"] == "not_reviewed"
    )
    bad_page = await httpx_client.get(path + "/samples?limit=101", headers=headers)
    assert bad_page.status_code == 422


async def test_class_ranking_and_content_matching_filter_before_pagination(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, data = await version(
        db_session,
        owner,
        classes=["unused", "truck", "car"],
        specs=[
            {"content": "duplicate", "labels": ["car"] * 3},
            {"content": "duplicate", "task": "another-member", "labels": ["car"] * 3},
            {"content": "truck", "split": "val", "labels": ["truck"] * 2},
            {"content": "car", "split": "val", "labels": ["car"]},
        ],
    )
    path = f"/api/v1/projects/{project.id}/training-datasets/{data.id}/insights"
    headers = {"Authorization": "Bearer " + token}
    ranked = await httpx_client.get(
        path + "/classes?order_by=instances&limit=1", headers=headers
    )
    assert ranked.status_code == 200, ranked.text
    assert ranked.json()["items"][0]["name"] == "car"
    assert ranked.json()["total"] == 3
    assert ranked.json()["order_by"] == "instances"
    validation = await httpx_client.get(
        path + "/classes?order_by=instances&split=val&limit=1", headers=headers
    )
    assert validation.json()["items"][0]["name"] == "truck"
    assert validation.json()["split"] == "val"
    member_rank = await httpx_client.get(
        path + "/classes?order_by=images&limit=1&offset=1", headers=headers
    )
    assert member_rank.json()["items"][0]["name"] == "truck"
    exact = await httpx_client.get(
        path + f"/samples?content_sha256={sha('duplicate')}&limit=1&offset=1",
        headers=headers,
    )
    assert exact.status_code == 200, exact.text
    assert exact.json()["total"] == 2 and len(exact.json()["items"]) == 1
    assert exact.json()["items"][0]["sha256"] == sha("duplicate")
    unknown = await httpx_client.get(
        path + f"/samples?content_sha256={sha('not-retained')}", headers=headers
    )
    assert unknown.json()["total"] == 0
    invalid = await httpx_client.get(
        path + "/samples?content_sha256=not-a-content-hash", headers=headers
    )
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    "before, after, mixed_side, width, expected",
    [
        (None, "val", None, 0.3, (1, 0, 0, 0, 0)),
        (None, None, None, 0.3, (1, 0, 0, 0, 0)),
        ("train", "val", None, 0.3, (0, 1, 0, 0, 0)),
        ("val", "val", None, 0.3, (0, 0, 1, 0, 0)),
        ("train", "val", "baseline", 0.3, (1, 0, 0, 0, 1)),
        ("train", "val", "target", 0.3, (1, 0, 0, 0, 1)),
        ("train", "val", "target", 0.5, (1, 0, 0, 1, 1)),
    ],
)
async def test_split_evidence_comparison_filters_select_contents_without_inventing_changes(
    db_session, super_admin, httpx_client, before, after, mixed_side, width, expected
):
    owner, token = super_admin
    before_specs = [{"content": "same", "split": before}]
    after_specs = [{"content": "same", "split": after, "w": width}]
    if mixed_side == "baseline":
        before_specs.append(
            {"content": "same", "task": "unknown-baseline", "split": None}
        )
    if mixed_side == "target":
        after_specs.append(
            {"content": "same", "task": "unknown-target", "split": None, "w": width}
        )
    project, baseline = await version(db_session, owner, specs=before_specs)
    _, target = await version(db_session, owner, project=project, specs=after_specs)
    path = f"/api/v1/projects/{project.id}/training-datasets/{target.id}/insights"
    headers = {"Authorization": "Bearer " + token}
    response = await httpx_client.get(
        path + f"/compare?baseline_id={baseline.id}", headers=headers
    )
    assert response.status_code == 200, response.text
    for change, count in zip(
        (
            "unknown",
            "split_changed",
            "unchanged",
            "labels_changed",
            "multiplicity_changed",
        ),
        expected,
        strict=True,
    ):
        assert response.json()["counts"][change] == count
        selected = await httpx_client.get(
            path + f"/samples?baseline_id={baseline.id}&change={change}",
            headers=headers,
        )
        assert selected.status_code == 200, selected.text
        assert selected.json()["source_version_id"] == str(target.id)
        assert selected.json()["total"] == (len(after_specs) if count else 0)
        assert all(
            sample["sha256"] == sha("same") for sample in selected.json()["items"]
        )


async def test_comparison_scope_removed_rows_and_cache_rebuild_then_cascade(
    db_session, super_admin, annotator, httpx_client, monkeypatch
):
    owner, token = super_admin
    project, baseline = await version(
        db_session,
        owner,
        specs=[{"content": "removed"}, {"content": "kept", "split": "val"}],
    )
    _, target = await version(
        db_session,
        owner,
        project=project,
        specs=[{"content": "added"}, {"content": "kept", "split": "val"}],
    )
    other_project, foreign = await version(db_session, owner)
    headers = {"Authorization": "Bearer " + token}
    path = f"/api/v1/projects/{project.id}/training-datasets/{target.id}/insights"
    response = await httpx_client.get(
        path + f"/compare?baseline_id={baseline.id}", headers=headers
    )
    assert response.status_code == 200, response.text
    assert (
        response.json()["counts"]["removed"] == response.json()["counts"]["added"] == 1
    )
    removed = await httpx_client.get(
        path + f"/samples?change=removed&baseline_id={baseline.id}", headers=headers
    )
    assert removed.json()["source_version_id"] == str(baseline.id)
    assert removed.json()["items"][0]["version_id"] == str(baseline.id)
    assert (
        await httpx_client.get(
            path + f"/compare?baseline_id={foreign.id}", headers=headers
        )
    ).status_code == 404
    employee, employee_token = annotator
    await create_membership(
        db_session,
        project_id=project.id,
        user_id=employee.id,
        role="annotator",
        assigned_by=owner.id,
    )
    assert (
        await httpx_client.get(
            path, headers={"Authorization": "Bearer " + employee_token}
        )
    ).status_code == 403
    from app.services.api_key_service import create_key

    key, key_token = await create_key(db_session, owner, "analysis", ["datasets:read"])
    assert (
        await httpx_client.get(path, headers={"Authorization": "Bearer " + key_token})
    ).status_code == 403
    key.scopes = ["datasets:read", "annotations:read"]
    await db_session.flush()
    assert (
        await httpx_client.get(path, headers={"Authorization": "Bearer " + key_token})
    ).status_code == 200
    cached = await db_session.get(TrainingDatasetAnalysis, target.id)
    cached.input_digest = "0" * 64
    await db_session.flush()
    regenerated = await httpx_client.get(path, headers=headers)
    assert regenerated.status_code == 200, regenerated.text
    assert regenerated.json()["input_digest"] != "0" * 64
    assert regenerated.json()["counts"]["images"] == 2
    monkeypatch.setattr(service.storage, "delete_prefix", lambda *args, **kwargs: 0)
    removed = await httpx_client.delete(path.removesuffix("/insights"), headers=headers)
    assert removed.status_code == 204, removed.text
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TrainingDatasetAnalysis)
            .where(TrainingDatasetAnalysis.version_id == target.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TrainingDatasetAnalyzedSample)
            .where(TrainingDatasetAnalyzedSample.version_id == target.id)
        )
        == 0
    )


class Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class FrozenStore:
    def __init__(self, payload, digest, snapshot_digest):
        from types import SimpleNamespace

        from app.config import settings

        self.meta = SimpleNamespace(
            endpoint_url=f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        )
        self.payload, self.digest, self.snapshot_digest = (
            payload,
            digest,
            snapshot_digest,
        )
        self.missing = False
        self.calls = []
        self.bodies = []

    def head_object(self, **kwargs):
        self.calls.append(kwargs)
        if self.missing:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        return {
            "ContentLength": len(self.payload),
            "ETag": '"frozen"',
            "Metadata": {
                "sha256": self.digest,
                "snapshot-digest": self.snapshot_digest,
            },
        }

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        body = Body(self.payload)
        self.bodies.append(body)
        return {"Body": body}


async def test_immutable_media_checks_bytes_and_never_substitutes_current_source(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(output, format="PNG")
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    project, data = await version(
        db_session,
        owner,
        specs=[
            {"content": "frozen", "sha256": digest, "size_bytes": len(payload)},
            {"content": "other", "split": "val"},
        ],
    )
    fake = FrozenStore(payload, digest, data.snapshot_digest)
    monkeypatch.setattr(service.storage, "client", fake)
    from app.services import training_storage_io, training_dataset_storage

    async def capture_fixture(db, owner, **kwargs):
        return await training_dataset_storage.target_for_ref(db, owner.ref, **kwargs)

    monkeypatch.setattr(training_storage_io, "prepare", capture_fixture)
    headers = {"Authorization": "Bearer " + token}
    base = f"/api/v1/projects/{project.id}/training-datasets/{data.id}/insights"
    task_id = data.manifest["samples"][0]["task_id"]
    path = base + f"/samples/{task_id}/media"
    response = await httpx_client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    assert (
        response.content == payload and response.headers["cache-control"] == "no-store"
    )
    assert all(
        c["Bucket"] == service.storage.training_bucket
        and c["Key"] == f"projects/{project.id}/datasets/{data.id}/media/{task_id}"
        for c in fake.calls
    )
    assert all(body.closed for body in fake.bodies)
    fake.payload = payload[:-1] + bytes([payload[-1] ^ 1])
    corrupted = await httpx_client.get(path, headers=headers)
    assert (
        corrupted.status_code == 409
        and corrupted.json()["detail"]["code"] == "frozen_media_digest"
    )
    assert all(body.closed for body in fake.bodies)
    fake.missing = True
    missing = await httpx_client.get(path, headers=headers)
    assert (
        missing.status_code == 404
        and missing.json()["detail"]["code"] == "frozen_media_unavailable"
    )
    observed = (await httpx_client.get(base, headers=headers)).json()["media"]
    assert (
        observed["unavailable"] == 1
        and observed["unchecked"] == 1
        and observed["verified"] == 0
    )
    assert observed["last_checked_at"]
    assert (
        await httpx_client.get(base + f"/samples/{uuid.uuid4()}/media", headers=headers)
    ).status_code == 404


async def test_preview_respects_frozen_exif_dimensions_and_paginated_native_labels(
    db_session, super_admin, httpx_client, monkeypatch
):
    owner, token = super_admin
    output = io.BytesIO()
    image = Image.new("RGB", (12, 16), "blue")
    exif = Image.Exif()
    exif[274] = 6
    image.save(output, format="JPEG", exif=exif)
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    project, data = await version(
        db_session,
        owner,
        specs=[
            {
                "content": "oriented",
                "sha256": digest,
                "size_bytes": len(payload),
                "labels": ["car", "truck"],
            },
            {"content": "negative", "labels": [], "split": "val"},
        ],
    )
    fake = FrozenStore(payload, digest, data.snapshot_digest)
    monkeypatch.setattr(service.storage, "client", fake)
    from app.services import training_storage_io, training_dataset_storage

    async def capture_fixture(db, owner, **kwargs):
        return await training_dataset_storage.target_for_ref(db, owner.ref, **kwargs)

    monkeypatch.setattr(training_storage_io, "prepare", capture_fixture)
    headers = {"Authorization": "Bearer " + token}
    task_id = data.manifest["samples"][0]["task_id"]
    path = f"/api/v1/projects/{project.id}/training-datasets/{data.id}/insights/samples/{task_id}"
    preview = await httpx_client.get(path + "/media?preview=true", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(preview.content)) as decoded:
        assert decoded.size == (16, 12)
    detail = await httpx_client.get(path + "?limit=1&offset=1", headers=headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["total"] == 2 and len(detail.json()["labels"]) == 1
    assert detail.json()["labels"][0] == {
        "type": "bbox",
        "class_index": 1,
        "class_name": "truck",
        "x_center": 0.25,
        "y_center": 0.4,
        "width": 0.3,
        "height": 0.4,
    }


def test_large_metadata_derivation_keeps_public_summary_constant_size():
    value = derived(
        [
            {"content": f"large-{i}", "split": "val" if i % 5 == 0 else "train"}
            for i in range(10000)
        ]
    )
    assert value["overview"]["counts"]["images"] == 10000
    assert len(value["samples"]) == 10000
    assert len(json.dumps(value["overview"])) < 30000


@pytest.mark.parametrize("storage_missing", [False, True])
async def test_media_observation_serializes_with_concurrent_version_deletion(
    test_engine, monkeypatch, storage_missing
):
    import asyncio
    import threading

    from fastapi import HTTPException
    from sqlalchemy import delete, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.api.v1.training_dataset_analysis import sample_media
    from app.api.v1.training_datasets import delete_version
    from app.db.models.project import Project
    from app.db.models.user import User
    from tests.factory import create_user

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    output = io.BytesIO()
    Image.new("RGB", (16, 12), "red").save(output, format="PNG")
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    async with maker() as seed:
        owner = await create_user(
            seed, "super_admin", f"media-race-{uuid.uuid4()}@test.local", "Media race"
        )
        project, data = await version(
            seed,
            owner,
            specs=[{"content": "race", "sha256": digest, "size_bytes": len(payload)}],
        )
        await seed.commit()
        await service.ensure_analysis(seed, project.id, data.id)

    entered, release = threading.Event(), threading.Event()
    fake = FrozenStore(payload, digest, data.snapshot_digest)
    original_read = fake.get_object

    def blocked_read(**kwargs):
        entered.set()
        assert release.wait(timeout=10), "Test did not release frozen media read"
        if storage_missing:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return original_read(**kwargs)

    fake.get_object = blocked_read
    monkeypatch.setattr(service.storage, "client", fake)
    deleted_prefixes = []
    monkeypatch.setattr(
        service.storage,
        "delete_prefix",
        lambda *args, **kwargs: deleted_prefixes.append(args[0]),
    )
    deletion_pid = None

    async def reading():
        async with maker() as db:
            try:
                return await sample_media(
                    data.id,
                    uuid.UUID(data.manifest["samples"][0]["task_id"]),
                    project,
                    owner,
                    db,
                    False,
                )
            except HTTPException as exc:
                return exc

    async def deleting():
        nonlocal deletion_pid
        async with maker() as db:
            deletion_pid = await db.scalar(text("select pg_backend_pid()"))
            await delete_version(data.id, project, owner, db)

    media_task = asyncio.create_task(reading())
    deletion_task = None
    response = None
    try:
        assert await asyncio.to_thread(entered.wait, 5), (
            "Media request did not enter storage read"
        )
        deletion_task = asyncio.create_task(deleting())

        async def wait_for_delete_lock():
            async with maker() as observer:
                while deletion_pid is None or not await observer.scalar(
                    text("select cardinality(pg_blocking_pids(:pid)) > 0"),
                    {"pid": deletion_pid},
                ):
                    await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_delete_lock(), timeout=5)
        assert not deletion_task.done() and not deleted_prefixes
        release.set()
        response = await asyncio.wait_for(media_task, timeout=5)
        await asyncio.wait_for(deletion_task, timeout=5)
        if storage_missing:
            assert isinstance(response, HTTPException) and response.status_code == 404
        else:
            assert response.status_code == 200
            assert b"".join([part async for part in response.body_iterator]) == payload
        assert len(deleted_prefixes) == 1
        async with maker() as verify:
            assert await verify.get(TrainingDatasetVersion, data.id) is None
            assert await verify.get(TrainingDatasetAnalysis, data.id) is None
    finally:
        release.set()
        await asyncio.gather(
            media_task,
            *([deletion_task] if deletion_task else []),
            return_exceptions=True,
        )
        if response is not None and not isinstance(response, HTTPException):
            await response.background()
        async with maker() as cleanup:
            await cleanup.execute(
                delete(TrainingDatasetVersion).where(
                    TrainingDatasetVersion.id == data.id
                )
            )
            await cleanup.execute(delete(Project).where(Project.id == project.id))
            await cleanup.execute(delete(User).where(User.id == owner.id))
            await cleanup.commit()
