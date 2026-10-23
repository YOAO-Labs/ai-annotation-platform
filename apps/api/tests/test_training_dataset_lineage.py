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
    }
