"""Frozen provenance changes independently of training semantics and live sources."""

import copy
import uuid

import pytest

from app.services import training_dataset_analysis as analysis
from app.services.training_dataset_bridge import TrainingDatasetError, content_digest
from tests.test_training_dataset_analysis import inputs, sha, version


def identifier(value):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def lineage(tag="source"):
    return {
        "source_id": identifier(tag),
        "location_id": identifier("location"),
        "path_digest": sha("private-path"),
        "scan_run_id": identifier("scan"),
        "accepted_revision_id": identifier("accepted"),
        "revision_number": 1,
        "previous_revision_id": None,
        "accepted_at": "2026-10-23T10:00:00+00:00",
        "scan_completed_at": "2026-10-23T09:00:00+00:00",
        "content_verified_at": "2026-10-23T09:30:00+00:00",
        "source_config_revision": 1,
        "location_config_revision": 1,
    }


def attach(snapshot, manifest, evidence):
    contract = {"schema": 1, "scope": "accepted-reference"}
    for record in (snapshot, manifest):
        record["source_lineage_contract"] = contract
        for index, member in enumerate(record["samples"]):
            member["asset_reference"] = {
                "schema": 1,
                "dataset_id": identifier("dataset"),
                "dataset_item_id": identifier(member["task_id"]),
                "asset_id": identifier("asset"),
                "sha256": sha("content"),
                "size_bytes": 128,
            }
            member["source_lineage"] = copy.deepcopy(
                evidence[index] if isinstance(evidence, list) else evidence
            )
    manifest["source_digest"] = content_digest(snapshot)
    manifest["validation_digest"] = content_digest(
        [member for member in manifest["samples"] if member["split"] == "val"]
    )
    manifest.pop("content_digest", None)
    manifest["content_digest"] = content_digest(manifest)


def derive(evidence):
    snapshot, manifest = inputs([{"content": "same", "split": "val"}])
    if evidence is not None:
        attach(snapshot, manifest, evidence)
    return analysis.derive(snapshot, manifest, None, content_digest(snapshot))


@pytest.mark.parametrize(
    "field,value,kind",
    [
        ("source_id", identifier("another"), "source_relation_changed"),
        ("location_id", identifier("another"), "source_relation_changed"),
        ("path_digest", sha("another"), "source_relation_changed"),
        ("accepted_revision_id", identifier("next"), "source_evidence_updated"),
        ("content_verified_at", "2026-10-24T10:00:00+00:00", "source_evidence_updated"),
        ("source_config_revision", 2, "source_evidence_updated"),
    ],
)
def test_source_change_is_not_a_training_semantic_change(field, value, kind):
    before = lineage()
    after = {**before, field: value}
    result = analysis.compare(derive(before), derive(after))
    assert result["counts"][kind] == 1
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["source_unknown"] == 0
    assert result["source_comparison_status"] == "complete"
    assert result["counts"]["labels_changed"] == result["counts"]["split_changed"] == 0
    assert result["semantic_validation_changed"] is False
    assert result["evaluation_benchmark_changed"] is False


@pytest.mark.parametrize("side", ["baseline", "target", "both"])
def test_missing_or_partial_provenance_cannot_establish_source_equality(side):
    partial = {**lineage(), "content_verified_at": None}
    before = derive(None if side in {"baseline", "both"} else lineage())
    after = derive(partial if side in {"target", "both"} else lineage())
    result = analysis.compare(before, after)
    assert result["counts"]["source_unknown"] == 1
    assert result["counts"]["source_relation_changed"] == 0
    assert result["counts"]["source_evidence_updated"] == 0
    assert result["counts"]["unchanged"] == 1
    assert result["status"] == "complete"
    assert result["source_comparison_status"] == "partial"


def test_source_comparison_keeps_duplicate_member_multisets_and_ignores_order():
    records = inputs([{"content": "same"}, {"content": "same", "task": "duplicate"}])
    attach(*records, [lineage("one"), lineage("two")])
    before = analysis.derive(*records, None, content_digest(records[0]))
    reordered = copy.deepcopy(records)
    for record in reordered:
        record["samples"].reverse()
    after = analysis.derive(*reordered, None, content_digest(reordered[0]))
    assert analysis.compare(before, after)["counts"]["source_relation_changed"] == 0
    changed = copy.deepcopy(records)
    for record in changed:
        record["samples"][1]["source_lineage"] = lineage("one")
    result = analysis.compare(
        before, analysis.derive(*changed, None, content_digest(changed[0]))
    )
    assert result["counts"]["source_relation_changed"] == 1
    assert result["counts"]["multiplicity_changed"] == 0


def test_lineage_cannot_be_downgraded_or_changed_between_snapshot_and_manifest():
    snapshot, manifest = inputs([{"content": "same"}])
    attach(snapshot, manifest, lineage())
    manifest["samples"][0]["source_lineage"]["path_digest"] = sha("wrong")
    with pytest.raises(TrainingDatasetError, match="冻结来源证据与清单不一致"):
        analysis.derive(snapshot, manifest, None, content_digest(snapshot))
    manifest.pop("source_lineage_contract")
    manifest["samples"][0].pop("source_lineage")
    with pytest.raises(TrainingDatasetError, match="冻结来源合同与清单不一致"):
        analysis.derive(snapshot, manifest, None, content_digest(snapshot))


def freeze_metadata(value, evidence):
    snapshot, manifest = copy.deepcopy(value.snapshot), copy.deepcopy(value.manifest)
    attach(snapshot, manifest, evidence)
    value.snapshot, value.manifest = snapshot, manifest
    value.snapshot_digest = content_digest(snapshot)


async def test_source_filters_page_all_frozen_members_and_reject_foreign_baseline(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    specs = [
        {"content": "same", "split": "val"},
        {"content": "same", "task": "duplicate", "split": "val"},
    ]
    project, before = await version(db_session, owner, specs=specs)
    _, after = await version(db_session, owner, project=project, specs=specs)
    freeze_metadata(before, [lineage(), lineage()])
    freeze_metadata(after, [lineage(), lineage("different")])
    await db_session.flush()
    headers = {"Authorization": "Bearer " + token}
    path = f"/api/v1/projects/{project.id}/training-datasets/{after.id}/insights"
    response = await httpx_client.get(
        path + f"/compare?baseline_id={before.id}", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["source_relation_changed"] == 1
    filtered = await httpx_client.get(
        path
        + f"/samples?baseline_id={before.id}&change=source_relation_changed&limit=1&offset=1",
        headers=headers,
    )
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["total"] == 2 and len(filtered.json()["items"]) == 1
    task_id = filtered.json()["items"][0]["task_id"]
    detail = await httpx_client.get(path + f"/samples/{task_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    evidence = detail.json()["source_lineage"]
    assert evidence["status"] == "recorded" and evidence["reasons"] == []
    assert evidence["evidence"]["source_id"] in {
        identifier("source"),
        identifier("different"),
    }
    # These IDs have never existed as live Source/Location/Task rows.
    # Serving them proves the read is over retained frozen metadata alone.
    _, foreign = await version(db_session, owner)
    await db_session.flush()
    denied = await httpx_client.get(
        path + f"/samples?baseline_id={foreign.id}&change=source_relation_changed",
        headers=headers,
    )
    assert denied.status_code == 404


async def test_historical_detail_is_explicitly_unknown_and_not_fabricated(
    db_session, super_admin, httpx_client
):
    owner, token = super_admin
    project, data = await version(db_session, owner, preparation=True)
    task_id = data.manifest["samples"][0]["task_id"]
    path = f"/api/v1/projects/{project.id}/training-datasets/{data.id}/insights/samples/{task_id}"
    response = await httpx_client.get(
        path, headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200, response.text
    evidence = response.json()["source_lineage"]
    assert evidence["status"] == "unrecorded" and evidence["evidence"] is None
    assert evidence["reasons"]
    base = path.rsplit("/samples/", 1)[0]
    excluded = await httpx_client.get(
        base + "/samples?kind=excluded", headers={"Authorization": "Bearer " + token}
    )
    assert excluded.status_code == 200 and excluded.json()["total"] == 1
    excluded_id = excluded.json()["items"][0]["task_id"]
    detail = await httpx_client.get(
        base + f"/samples/{excluded_id}", headers={"Authorization": "Bearer " + token}
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["sample"]["included"] is False
    assert detail.json()["source_lineage"] == {
        "status": "unrecorded",
        "reasons": ["not_in_package"],
        "evidence": None,
        "kind": "unknown",
        "managed_evidence": None,
    }


def attach_managed(snapshot, manifest, receipts=None):
    snapshot["managed_original_contract"] = 1
    for index, (source, member) in enumerate(
        zip(snapshot["samples"], manifest["samples"], strict=True)
    ):
        if member.get("asset_reference") is not None:
            continue
        source["original"] = {
            "schema": 1,
            "owner_kind": "dataset_asset",
            "content_id": identifier("original"),
            "location_id": identifier("managed-location"),
            "sha256": member["sha256"],
            "size_bytes": member["size_bytes"],
            **((receipts[index] if isinstance(receipts, list) else receipts) or {}),
        }
    manifest["source_digest"] = content_digest(snapshot)
    manifest["content_digest"] = content_digest(
        {key: value for key, value in manifest.items() if key != "content_digest"}
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_kind", "task_original"),
        ("content_id", identifier("another-content")),
        ("location_id", identifier("another-location")),
    ],
)
def test_managed_frozen_identity_changes_independently_of_training_semantics(
    field, value
):
    before = inputs([{"content": "same", "split": "val"}])
    attach_managed(*before)
    after = copy.deepcopy(before)
    attach_managed(*after, {field: value})
    a, b = [
        analysis.derive(*record, None, content_digest(record[0]))
        for record in (before, after)
    ]
    result = analysis.compare(a, b)
    assert result["counts"]["source_relation_changed"] == 1
    assert result["counts"]["source_unknown"] == 0
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["labels_changed"] == result["counts"]["split_changed"] == 0
    assert result["semantic_validation_changed"] is False
    assert result["evaluation_benchmark_changed"] is False


@pytest.mark.parametrize(
    "bad", ["extra", "sha", "size", "contract", "source_digest", "duplicate_task"]
)
def test_explicit_managed_proof_cannot_mismatch_or_expose_private_fields(bad):
    snapshot, manifest = inputs([{"content": "same"}])
    attach_managed(snapshot, manifest)
    if bad == "extra":
        snapshot["samples"][0]["original"]["bucket"] = "private-bucket"
    elif bad == "sha":
        snapshot["samples"][0]["original"]["sha256"] = sha("wrong")
    elif bad == "size":
        snapshot["samples"][0]["original"]["size_bytes"] += 1
    elif bad == "contract":
        snapshot["managed_original_contract"] = True
    elif bad == "duplicate_task":
        snapshot["samples"].append(copy.deepcopy(snapshot["samples"][0]))
    manifest["source_digest"] = (
        sha("wrong") if bad == "source_digest" else content_digest(snapshot)
    )
    with pytest.raises(TrainingDatasetError, match="托管"):
        analysis.derive(snapshot, manifest, None, content_digest(snapshot))


def test_managed_source_multiset_preserves_duplicates_and_mixed_reference_evidence():
    before = inputs([{"content": "same"}, {"content": "same", "task": "duplicate"}])
    attach(*before, [lineage(), lineage("another")])
    # Keep one reference and one already frozen managed original in the same version.
    for record in before:
        record["samples"][1].pop("asset_reference", None)
        record["samples"][1].pop("source_lineage", None)
    attach_managed(*before)
    after = copy.deepcopy(before)
    after[0]["samples"][1]["original"]["location_id"] = identifier("changed")
    after[1]["source_digest"] = content_digest(after[0])
    a, b = [
        analysis.derive(*record, None, content_digest(record[0]))
        for record in (before, after)
    ]
    assert analysis.compare(a, b)["counts"]["source_relation_changed"] == 1
    assert analysis.compare(a, b)["counts"]["multiplicity_changed"] == 0
    for record in before:
        record["samples"].reverse()
    before[1]["source_digest"] = content_digest(before[0])
    reordered = analysis.derive(*before, None, content_digest(before[0]))
    assert analysis.compare(a, reordered)["counts"]["source_relation_changed"] == 0


@pytest.mark.parametrize("owner_kind", ["dataset_asset", "task_original"])
async def test_managed_detail_uses_frozen_projection_and_does_not_resolve_live_original(
    db_session, super_admin, httpx_client, monkeypatch, owner_kind
):
    from app.services import original_sources

    owner, token = super_admin
    project, data = await version(db_session, owner)
    snapshot, manifest = copy.deepcopy(data.snapshot), copy.deepcopy(data.manifest)
    attach_managed(snapshot, manifest, {"owner_kind": owner_kind})
    data.snapshot, data.manifest, data.snapshot_digest = (
        snapshot,
        manifest,
        content_digest(snapshot),
    )
    await db_session.flush()

    async def unexpected(*args, **kwargs):
        raise AssertionError(
            "Frozen explanation must not resolve a current Task/Source"
        )

    monkeypatch.setattr(original_sources, "resolve_task", unexpected)
    task = manifest["samples"][0]["task_id"]
    path = f"/api/v1/projects/{project.id}/training-datasets/{data.id}/insights/samples/{task}"
    response = await httpx_client.get(
        path, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200, response.text
    source = response.json()["source_lineage"]
    assert source["kind"] == "managed_original" and source["status"] == "recorded"
    assert source["managed_evidence"] == snapshot["samples"][0]["original"]
    assert source["evidence"] is None
    assert "source_key" not in response.text and "mutable-inputs" not in response.text
    assert data.snapshot == snapshot and data.manifest == manifest


@pytest.mark.parametrize("changed", [None, "sha256", "size_bytes", "included"])
async def test_cache_schema_upgrade_preserves_only_matching_media_observations(
    db_session, super_admin, changed
):
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.db.models.training_dataset_analysis import TrainingDatasetAnalyzedSample

    owner, _ = super_admin
    project, data = await version(db_session, owner)
    _, cached, _ = await analysis.ensure_analysis(db_session, project.id, data.id)
    row = await db_session.scalar(
        select(TrainingDatasetAnalyzedSample).where(
            TrainingDatasetAnalyzedSample.version_id == data.id
        )
    )
    row.media_state, row.media_checked_at = (
        "verified",
        datetime(2026, 10, 24, tzinfo=timezone.utc),
    )
    if changed == "sha256":
        row.sha256 = sha("other")
    elif changed == "size_bytes":
        row.size_bytes += 1
    elif changed == "included":
        row.included = not row.included
    cached.input_digest = sha("previous-schema")
    await db_session.flush()
    await analysis.ensure_analysis(db_session, project.id, data.id)
    await db_session.refresh(row)
    assert row.media_state == ("verified" if changed is None else "unchecked")
    assert (row.media_checked_at is not None) is (changed is None)
