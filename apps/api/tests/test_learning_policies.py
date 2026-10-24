"""Automatic training: semantic growth, current authority and durable scheduling."""

import copy
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import func, select
from app.config import settings
from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun
from app.services import learning_policies as service, training_runs as runs
from app.services.training_dataset_bridge import semantic_partitions
from tests.test_training_runs import ready_dataset, executor_claim

from tests.test_training_runs import training_config as training_config_fixture  # noqa: F401

pytestmark = pytest.mark.usefixtures("training_config_fixture")


async def test_v2_policy_adoption_is_explicit_and_children_replay_pinned_effective_values(
    db_session, super_admin, httpx_client, frozen_source
):
    from training_contract import definition

    owner, token = super_admin
    project, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    legacy = copy.deepcopy(policy.config)
    assert "configuration" not in legacy
    assert service.template_current(policy)
    schema = definition("ultralytics", version="8.4.177")
    update = {key: value for key, value in body.items() if key != "parameters"}
    update.update(
        expected_revision=1,
        configuration={
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {
                "epochs": 4,
                "optimizer": "AdamW",
                "close_mosaic": 0,
                "mosaic": 0.5,
            },
        },
    )
    response = await httpx_client.put(
        path, json=update, headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200, response.text
    await db_session.refresh(policy)
    assert policy.revision == 2 and policy.config["parameters"]["epochs"] == 4
    pinned = copy.deepcopy(policy.config["configuration"])
    _, _, run = await build_candidate(db_session, policy)
    assert run.config["configuration"] == pinned
    assert run.config["adapter_revision"] == 2
    assert run.config["parameters"] == policy.config["parameters"]


def manifest(train=None, val="v1", classes=None):
    return {
        "schema": 1,
        "classes": classes or ["car"],
        "content_digest": "c" * 64,
        "validation_digest": "d" * 64,
        "samples": [
            {
                "task_id": str(uuid.uuid4()),
                "sha256": sha,
                "label_sha256": label,
                "split": "train",
            }
            for sha, label in (train or {"a": "label-a", "b": "label-b"}).items()
        ]
        + [
            {
                "task_id": str(uuid.uuid4()),
                "sha256": "val-image",
                "label_sha256": val,
                "split": "val",
            }
        ],
    }


@pytest.fixture
def frozen_source(monkeypatch):
    source = {"revision": 1}

    async def prepare(db, project, selection, **kw):
        assert kw["trusted_actor"] is True
        assert not db.in_transaction(), "Physical reads retained a business transaction"
        snapshot = {
            "schema": 1,
            "classes": ["car"],
            "samples": [],
            "seed": 42,
            "validation_fraction": 0.2,
            "selection": selection.model_dump(mode="json"),
            "authorization": {
                "actor_id": str(kw["actor_id"]),
                "api_key_id": kw.get("api_key_id"),
            },
            "source_revision": source["revision"],
        }
        report = {
            "ready": True,
            "issues": [],
            "excluded": [],
            "digest": service.content_digest(snapshot),
            "summary": {
                "selected": 0,
                "eligible": 0,
                "positives": 0,
                "negatives": 0,
                "excluded": 0,
            },
        }
        if source.get("during_prepare"):
            await source["during_prepare"]()
        return SimpleNamespace(snapshot=snapshot, report=report)

    async def finalize(db, prepared):
        source["finalized"] = source.get("finalized", 0) + 1
        return prepared.snapshot, prepared.report

    monkeypatch.setattr(service.datasets, "prepare_selection", prepare, raising=False)
    monkeypatch.setattr(service.datasets, "finalize_selection", finalize, raising=False)
    monkeypatch.setattr(
        service.datasets, "check_prepared_clock", lambda prepared: None, raising=False
    )
    monkeypatch.setattr(service.datasets, "dispatch_dataset", AsyncMock())
    return source


async def setup_policy(db, client, owner, token, *, enabled=True, threshold=1):
    project, parent = await ready_dataset(db, owner)
    parent.manifest = manifest()
    await db.flush()
    body = {
        "expected_revision": 0,
        "enabled": enabled,
        "parent_id": str(parent.id),
        "base_model": "yolo11n",
        "parameters": {"epochs": 3},
        "min_changed_samples": threshold,
        "check_interval_minutes": 1,
        "cooldown_minutes": 0,
    }
    path = f"/api/v1/projects/{project.id}/learning-policy"
    response = await client.put(
        path, json=body, headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200, response.text
    return (
        project,
        parent,
        await db.get(LearningPolicy, uuid.UUID(response.json()["id"])),
        path,
        body,
    )


@pytest.mark.parametrize(
    "changed_field",
    [
        "revision",
        "task",
        "config",
        "authorization",
        "creator_id",
        "baseline_dataset_id",
        "baseline",
        "blocked_train_digest",
        "last_snapshot_digest",
        "enabled",
        "next_check_at",
        "last_started_at",
        "state",
        "message",
    ],
)
async def test_preparation_discards_edited_policy_without_overwriting_new_state(
    db_session,
    super_admin,
    httpx_client,
    frozen_source,
    changed_field,
):
    owner, token = super_admin
    _, parent, policy, _, _ = await setup_policy(db_session, httpx_client, owner, token)
    edited = None

    async def edit_during_preparation():
        nonlocal edited
        values = {
            "revision": policy.revision + 1,
            "task": "segment",
            "config": {**policy.config, "min_changed_samples": 13},
            "authorization": {"api_key_id": str(uuid.uuid4())},
            "creator_id": None,
            "baseline_dataset_id": parent.id,
            "baseline": {"new": "baseline"},
            "blocked_train_digest": "b" * 64,
            "last_snapshot_digest": "c" * 64,
            "enabled": False,
            "next_check_at": runs.now() + timedelta(hours=1),
            "last_started_at": runs.now(),
            "state": "suspended",
            "message": "A newer policy editor owns this state",
        }
        assert getattr(policy, changed_field) != values[changed_field]
        setattr(policy, changed_field, values[changed_field])
        await db_session.flush()
        edited = service._policy_context(policy)

    frozen_source["during_prepare"] = edit_during_preparation
    await service.check_policy(db_session, policy.id, force=True)
    assert service._policy_context(policy) == edited
    assert frozen_source.get("finalized", 0) == 0
    assert not await db_session.scalar(
        select(LearningTrigger.id).where(LearningTrigger.policy_id == policy.id)
    )


async def test_newer_checker_publishes_once_while_older_preparation_is_in_flight(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    _, _, policy, _, _ = await setup_policy(db_session, httpx_client, owner, token)
    newer = None

    async def check_again():
        nonlocal newer
        frozen_source.pop("during_prepare")
        await service.check_policy(db_session, policy.id, force=True)
        await db_session.flush()
        newer = service._policy_context(policy)

    frozen_source["during_prepare"] = check_again
    await service.check_policy(db_session, policy.id, force=True)
    assert newer is not None and service._policy_context(policy) == newer
    assert frozen_source["finalized"] == 1
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(LearningTrigger)
            .where(LearningTrigger.policy_id == policy.id)
        )
        == 1
    )


async def test_other_task_trigger_started_during_preparation_blocks_publication(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    project, parent, policy, _, _ = await setup_policy(
        db_session, httpx_client, owner, token
    )

    async def start_other_task():
        other = LearningPolicy(
            project_id=project.id,
            creator_id=owner.id,
            task="segment",
            config=copy.deepcopy(policy.config),
            authorization={},
            enabled=True,
        )
        db_session.add(other)
        await db_session.flush()
        db_session.add(
            LearningTrigger(
                policy_id=other.id,
                project_id=project.id,
                policy_revision=other.revision,
                dataset_id=parent.id,
                source_digest="d" * 64,
            )
        )
        await db_session.flush()

    frozen_source["during_prepare"] = start_other_task
    await service.check_policy(db_session, policy.id, force=True)
    assert policy.state == "active" and "另一个任务" in policy.message
    assert frozen_source.get("finalized", 0) == 0
    assert not await db_session.scalar(
        select(LearningTrigger.id).where(LearningTrigger.policy_id == policy.id)
    )


async def test_authority_revoked_during_preparation_suspends_without_finalizing(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    _, _, policy, _, _ = await setup_policy(db_session, httpx_client, owner, token)

    async def revoke():
        owner.is_active = False
        await db_session.flush()

    frozen_source["during_prepare"] = revoke
    await service.check_policy(db_session, policy.id, force=True)
    assert not policy.enabled and policy.state == "suspended"
    assert frozen_source.get("finalized", 0) == 0
    assert not await db_session.scalar(
        select(LearningTrigger.id).where(LearningTrigger.policy_id == policy.id)
    )


async def test_preparation_authority_error_does_not_suspend_a_newer_policy_edit(
    db_session, super_admin, httpx_client, frozen_source
):
    from app.services.training_dataset_bridge import TrainingDatasetError

    owner, token = super_admin
    _, _, policy, _, _ = await setup_policy(db_session, httpx_client, owner, token)

    async def edit_then_fail():
        policy.revision += 1
        policy.message = "New editor state"
        await db_session.flush()
        raise TrainingDatasetError(
            "authority_revoked", "Old claim revoked", status_code=403
        )

    frozen_source["during_prepare"] = edit_then_fail
    await service.check_policy(db_session, policy.id, force=True)
    assert (
        policy.enabled and policy.revision == 2 and policy.message == "New editor state"
    )
    assert frozen_source.get("finalized", 0) == 0


async def test_task_policies_have_independent_revision_disable_and_history(
    db_session, super_admin, httpx_client, monkeypatch, frozen_source
):
    from training_contract import definition
    from training_contract.dataset import INSTANCE_RLE_CONTRACT

    owner, token = super_admin
    project, parent, detect, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    configured = copy.deepcopy(settings.training_catalog_json)
    configured["ultralytics"]["base_models"]["yolo11n-seg"] = {
        "filename": "yolo11n-seg.pt",
        "sha256": "9" * 64,
    }
    monkeypatch.setattr(settings, "training_catalog_json", configured)
    segment_parent = TrainingDatasetVersion(
        project_id=project.id,
        creator_id=owner.id,
        request_id=uuid.uuid4(),
        request_digest="1" * 64,
        snapshot_digest="2" * 64,
        name="Mask parent",
        status="ready",
        snapshot={**INSTANCE_RLE_CONTRACT, "samples": []},
        manifest={**manifest(), **INSTANCE_RLE_CONTRACT},
        package_key="fixture/masks.zip",
        package_sha256="3" * 64,
    )
    db_session.add(segment_parent)
    await db_session.flush()
    schema = definition("ultralytics", "yolo11n-seg", version="8.4.177")
    segment_body = {key: value for key, value in body.items() if key != "parameters"}
    segment_body.update(
        task="segment",
        parent_id=str(segment_parent.id),
        base_model="yolo11n-seg",
        configuration={
            "schema_id": schema["schema_id"],
            "schema_digest": schema["schema_digest"],
            "values": {"epochs": 3},
        },
    )
    auth = {"Authorization": "Bearer " + token}
    saved = await httpx_client.put(path, json=segment_body, headers=auth)
    assert saved.status_code == 200, saved.text
    assert saved.json()["task"] == "segment" and saved.json()["revision"] == 1
    segment = await db_session.get(LearningPolicy, uuid.UUID(saved.json()["id"]))
    assert segment.config["bridge"] == "ultralytics-instance-rle-1"
    assert segment.config["configuration"]["effective_values"]["workers"] == 0
    assert service.template_current(segment)
    assert (await httpx_client.get(path, headers=auth)).json()["id"] == str(detect.id)
    assert (
        await httpx_client.get(path, params={"task": "segment"}, headers=auth)
    ).json()["id"] == str(segment.id)
    # A building candidate for detection also excludes a segmentation build.
    trigger = LearningTrigger(
        policy_id=detect.id,
        project_id=project.id,
        policy_revision=1,
        dataset_id=parent.id,
        source_digest="4" * 64,
        status="building",
    )
    db_session.add(trigger)
    await db_session.flush()
    await service.check_policy(db_session, segment.id, force=True)
    assert segment.state == "active" and "另一个任务" in segment.message
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(LearningTrigger)
            .where(LearningTrigger.policy_id == segment.id)
        )
        == 0
    )
    disabled = await httpx_client.post(
        path + "/disable",
        params={"task": "segment", "expected_revision": 1},
        headers=auth,
    )
    assert disabled.status_code == 200 and disabled.json()["revision"] == 2
    await db_session.refresh(detect)
    assert detect.enabled and detect.revision == 1
    segment_history = await httpx_client.get(
        path + "/triggers", params={"task": "segment"}, headers=auth
    )
    detect_history = await httpx_client.get(path + "/triggers", headers=auth)
    assert segment_history.json()["total"] == 0 and detect_history.json()["total"] == 1


async def build_candidate(db, policy, value=None):
    await service.check_policy(db, policy.id, force=True)
    await db.flush()
    trigger = await db.scalar(
        select(LearningTrigger).where(
            LearningTrigger.policy_id == policy.id, LearningTrigger.status == "building"
        )
    )
    assert trigger
    data = await db.get(TrainingDatasetVersion, trigger.dataset_id)
    assert data.preparation_report["snapshot_digest"] == data.snapshot_digest
    assert data.preparation_report["schema"] == 1
    data.status, data.package_sha256, data.package_key = (
        "ready",
        "e" * 64,
        data.storage_ref["prefix"] + "dataset.zip",
    )
    data.manifest = value or manifest()
    await db.flush()
    await service.check_policy(db, policy.id, force=True)
    await db.flush()
    run = await db.scalar(
        select(TrainingRun).where(TrainingRun.auto_trigger_id == trigger.id)
    )
    return trigger, data, run


def test_semantic_identity_ignores_ids_paths_duplicates_and_validation_only_growth():
    a = manifest()
    b = copy.deepcopy(a)
    b["samples"].reverse()
    b["samples"].append({**b["samples"][0], "task_id": "another-task"})
    for s in b["samples"]:
        s.update(task_id=str(uuid.uuid4()), image_path="renamed.png", scene_id="other")
    assert semantic_partitions(a) == semantic_partitions(b)
    baseline = semantic_partitions(a)
    changed = manifest(val="corrected-validation")
    assert service.growth(semantic_partitions(changed), baseline) == 0
    assert (
        semantic_partitions(changed)["validation_digest"]
        != baseline["validation_digest"]
    )
    assert (
        service.growth(
            semantic_partitions(
                manifest({"a": "corrected", "b": "label-b", "new": "label"})
            ),
            baseline,
        )
        == 2
    )
    assert (
        service.growth(semantic_partitions(manifest(classes=["person"])), baseline) == 2
    )


async def test_policy_is_explicit_revisioned_and_guards_referenced_storage(
    db_session, super_admin, httpx_client, frozen_source, monkeypatch
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token, enabled=False
    )
    assert not policy.enabled and policy.revision == 1
    stale = await httpx_client.put(
        path, json=body, headers={"Authorization": "Bearer " + token}
    )
    assert stale.status_code == 409
    delete_objects = AsyncMock()
    monkeypatch.setattr(
        "app.api.v1.training_datasets.storage_service.delete_prefix", delete_objects
    )
    deleted = await httpx_client.delete(
        f"/api/v1/projects/{p.id}/training-datasets/{parent.id}",
        headers={"Authorization": "Bearer " + token},
    )
    assert deleted.status_code == 409
    delete_objects.assert_not_called()
    invalid = await httpx_client.put(
        path,
        json={**body, "expected_revision": 1, "min_changed_samples": 0},
        headers={"Authorization": "Bearer " + token},
    )
    assert invalid.status_code == 422
    assert (
        await httpx_client.delete(path, headers={"Authorization": "Bearer " + token})
    ).status_code == 204


async def test_pending_dispatch_recovery_reuses_attempt_and_overlapping_check_is_skipped(
    db_session, test_engine, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    # A second connection owns the coordination key even before any policy rows
    # are visible outside this test's enclosing rollback transaction.
    other_project, other_parent = await ready_dataset(db_session, owner)
    other_policy = LearningPolicy(
        project_id=other_project.id,
        creator_id=owner.id,
        config=policy.config,
        authorization={},
        enabled=True,
    )
    db_session.add(other_policy)
    await db_session.flush()
    async with test_engine.connect() as other:
        await other.execute(
            select(
                func.pg_advisory_lock(func.hashtextextended(str(other_project.id), 0))
            )
        )
        try:
            await service.check_policy(db_session, other_policy.id, force=True)
            assert (
                await db_session.scalar(
                    select(func.count()).select_from(LearningTrigger)
                )
                == 0
            )
        finally:
            await other.execute(
                select(
                    func.pg_advisory_unlock(
                        func.hashtextextended(str(other_project.id), 0)
                    )
                )
            )
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    trigger = await db_session.scalar(
        select(LearningTrigger).where(LearningTrigger.policy_id == policy.id)
    )
    dataset = await db_session.get(TrainingDatasetVersion, trigger.dataset_id)
    attempt = dataset.attempt_id
    await service.dispatch_pending(db_session, policy.id)
    await service.check_policy(db_session, policy.id, force=True)
    await service.dispatch_pending(db_session, policy.id)
    assert dataset.attempt_id == attempt
    assert (
        await db_session.scalar(select(func.count()).select_from(LearningTrigger)) == 1
    )
    calls = service.datasets.dispatch_dataset.call_args_list
    assert len(calls) == 2 and all(call.args[1].id == dataset.id for call in calls)


async def test_failure_retains_baseline_and_val_only_change_cannot_retry_gpu(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    policy.baseline = semantic_partitions(manifest({"a": "label-a"}))
    saved = copy.deepcopy(policy.baseline)
    t, data, run = await build_candidate(db_session, policy)
    assert (
        run and t.changed_samples == 1 and run.config["framework_version"] == "8.4.177"
    )
    run.status = "failed"
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert policy.baseline == saved and policy.blocked_train_digest == t.train_digest
    frozen_source["revision"] += 1
    t2, d2, r2 = await build_candidate(
        db_session, policy, manifest(val="val-correction")
    )
    assert r2 is None and t2.status == "skipped" and "失败" in t2.message
    assert policy.baseline == saved
    # An explicit save advances revision and permits a deliberate retry.
    r = await httpx_client.put(
        path,
        json={**body, "expected_revision": 1},
        headers={"Authorization": "Bearer " + token},
    )
    assert r.status_code == 200, r.text
    t3, d3, r3 = await build_candidate(db_session, policy)
    assert r3 and t3.policy_revision == 2


async def test_success_alone_advances_baseline_and_edits_during_training_survive(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    t, data, run = await build_candidate(db_session, policy)
    frozen_source["revision"] += 1
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert (
        await db_session.scalar(select(func.count()).select_from(LearningTrigger)) == 1
    )
    assert policy.baseline == {}
    run.status = "succeeded"
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert (
        policy.baseline_dataset_id == data.id
        and policy.baseline == semantic_partitions(data.manifest)
    )
    t2, d2, r2 = await build_candidate(
        db_session, policy, manifest({"a": "label-a", "b": "corrected"})
    )
    assert r2 and t2.changed_samples == 1
    assert run.config["dataset_id"] == str(data.id)


async def test_validation_only_candidate_does_not_trigger_training(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    policy.baseline = semantic_partitions(manifest())
    trigger, data, run = await build_candidate(
        db_session, policy, manifest(val="new-val")
    )
    assert run is None and trigger.changed_samples == 0 and trigger.status == "skipped"


@pytest.mark.parametrize("started", [False, True])
async def test_disable_cancels_only_unclaimed_training(
    db_session, super_admin, httpx_client, frozen_source, started
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    trigger, data, run = await build_candidate(db_session, policy)
    if started:
        assignment = await runs.claim(db_session, executor_claim())
        assert assignment["run_id"] == str(run.id)
    response = await httpx_client.post(
        path + "/disable?expected_revision=1",
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 200, response.text
    assert run.status == ("preparing" if started else "cancelled")
    assert not policy.enabled


async def test_claim_rechecks_policy_revision_even_when_tick_has_not_cancelled(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    trigger, data, run = await build_candidate(db_session, policy)
    policy.revision += 1
    await db_session.flush()
    assert await runs.claim(db_session, executor_claim()) is None
    assert run.status == "cancelled"


@pytest.mark.parametrize("reason", ["account", "project", "catalog"])
async def test_current_authority_status_and_fixed_catalog_stop_future_runs(
    db_session,
    super_admin,
    httpx_client,
    frozen_source,
    monkeypatch,
    reason,
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    if reason == "account":
        owner.is_active = False
    elif reason == "project":
        p.status = "completed"
    else:
        monkeypatch.setattr(
            settings,
            "training_catalog_json",
            {
                "ultralytics": {
                    "version": "different",
                    "image_digest": "sha256:" + "f" * 64,
                    "base_models": {
                        "yolo11n": {"sha256": "2" * 64, "filename": "yolo11n.pt"}
                    },
                }
            },
        )
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert not policy.enabled and policy.state == "suspended"
    assert not await db_session.scalar(select(LearningTrigger.id))


@pytest.mark.parametrize("media_type", ["video", None])
async def test_save_rejects_unqualified_legacy_parent_without_mutating_policy(
    db_session, super_admin, httpx_client, frozen_source, media_type
):
    owner, token = super_admin
    _, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    parent.manifest = {**parent.manifest, "media_type": media_type}
    await db_session.flush()
    original_config = copy.deepcopy(policy.config)
    response = await httpx_client.put(
        path,
        json={**body, "expected_revision": policy.revision},
        headers={"Authorization": "Bearer " + token},
    )
    assert response.status_code == 422, response.text
    await db_session.refresh(policy)
    assert policy.revision == 1 and policy.config == original_config


@pytest.mark.parametrize("media_type", ["video", None])
async def test_legacy_parent_with_unqualified_output_media_suspends_before_freezing(
    db_session, super_admin, httpx_client, frozen_source, monkeypatch, media_type
):
    owner, token = super_admin
    _, parent, policy, _, _ = await setup_policy(db_session, httpx_client, owner, token)
    parent.manifest = {**parent.manifest, "media_type": media_type}
    await db_session.flush()
    prepare = AsyncMock(
        side_effect=AssertionError("Unqualified parent must not prepare")
    )
    monkeypatch.setattr(service.datasets, "prepare_selection", prepare)
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert not policy.enabled and policy.state == "suspended"
    prepare.assert_not_awaited()
    assert (
        await db_session.scalar(
            select(LearningTrigger.id).where(LearningTrigger.policy_id == policy.id)
        )
        is None
    )


async def test_identical_snapshot_is_not_packaged_again_and_policy_delete_keeps_run_audit(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token, threshold=100
    )
    t, data, run = await build_candidate(db_session, policy)
    assert run is None
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert (
        await db_session.scalar(select(func.count()).select_from(LearningTrigger)) == 1
    )
    assert policy.state == "waiting"
    # Explicit revision permits the candidate; its run then preserves the audit.
    await httpx_client.put(
        path,
        json={**body, "expected_revision": 1, "min_changed_samples": 1},
        headers={"Authorization": "Bearer " + token},
    )
    t, data, run = await build_candidate(db_session, policy)
    assert run
    await httpx_client.post(
        path + "/disable?expected_revision=2",
        headers={"Authorization": "Bearer " + token},
    )
    response = await httpx_client.delete(
        path, headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 409


async def test_revoked_policy_still_reconciles_started_terminal_run(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    trigger, data, run = await build_candidate(db_session, policy)
    run.status = "training"
    await db_session.flush()
    owner.is_active = False
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert trigger.status == "running" and not policy.enabled
    run.status = "failed"
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert trigger.status == "failed" and policy.state == "suspended"
    assert policy.baseline == {}


async def test_api_key_revocation_and_nonmanager_access_are_enforced(
    db_session, super_admin, annotator, httpx_client, frozen_source
):
    from app.db.models.api_key import ApiKey

    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    key = ApiKey(
        user_id=owner.id,
        name="owned policy",
        key_prefix="ak_owned",
        key_hash="not-used",
        scopes=["annotations:read", "datasets:read"],
    )
    db_session.add(key)
    await db_session.flush()
    policy.authorization = {"api_key_id": str(key.id)}
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert not policy.enabled and policy.state == "suspended"
    r = await httpx_client.get(
        path, headers={"Authorization": "Bearer " + annotator[1]}
    )
    assert r.status_code in {403, 404}


async def test_resume_rejects_another_candidate_and_explicit_resume_can_continue_disabled_policy(
    db_session, super_admin, httpx_client, frozen_source, monkeypatch
):
    from app.db.models.training_run import TrainingAttempt

    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    t, data, run = await build_candidate(db_session, policy)
    run.status = "cancelled"
    attempt = await db_session.get(TrainingAttempt, run.current_attempt_id)
    attempt.status = "cancelled"
    attempt.artifacts = {
        "last.pt": {
            "role": "checkpoint",
            "epoch": 1,
            "key": f"projects/{p.id}/runs/{run.id}/attempts/{attempt.id}/last.pt",
            "sha256": "f" * 64,
            "size_bytes": 1,
        }
    }
    lease = await runs.lock_resource(db_session, run.config["resource_id"])
    lease.runtime = executor_claim().runtime
    monkeypatch.setattr(
        runs.storage.client,
        "head_object",
        lambda **kwargs: {
            "ContentLength": 1,
            "Metadata": {"attempt-id": str(attempt.id), "sha256": "f" * 64},
        },
    )
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    frozen_source["revision"] += 1
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    url = f"/api/v1/projects/{p.id}/training-runs/{run.id}/resume"
    r = await httpx_client.post(url, headers={"Authorization": "Bearer " + token})
    assert r.status_code == 409
    r = await httpx_client.post(
        path + "/disable?expected_revision=1",
        headers={"Authorization": "Bearer " + token},
    )
    assert r.status_code == 200
    r = await httpx_client.post(url, headers={"Authorization": "Bearer " + token})
    assert r.status_code == 202, r.text
    assignment = await runs.claim(db_session, executor_claim())
    assert assignment["run_id"] == str(run.id)
    assert assignment["resume"]["epoch"] == 1


async def test_bounded_tick_rotates_fairly_across_more_than_one_page(
    db_session, super_admin, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from tests.factory import create_project

    owner, _ = super_admin
    identities = []
    for _ in range(5):
        p = await create_project(db_session, owner_id=owner.id)
        policy = LearningPolicy(
            project_id=p.id,
            creator_id=owner.id,
            enabled=True,
            config={},
            authorization={},
        )
        db_session.add(policy)
        await db_session.flush()
        identities.append(policy.id)
    seen = []

    async def check(db, identity, **kw):
        seen.append(identity)
        policy = await db.get(LearningPolicy, identity)
        policy.last_checked_at = runs.now()

    monkeypatch.setattr(service, "CHECK_BATCH_SIZE", 2)
    monkeypatch.setattr(service, "check_policy", check)
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    for _ in range(3):
        await service.tick(factory)
    assert set(seen) == set(identities)


async def test_success_parent_advance_without_source_change_does_not_repackage(
    db_session, super_admin, httpx_client, frozen_source
):
    owner, token = super_admin
    p, parent, policy, path, body = await setup_policy(
        db_session, httpx_client, owner, token
    )
    trigger, data, run = await build_candidate(db_session, policy)
    run.status = "succeeded"
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    await db_session.flush()
    assert (
        await db_session.scalar(select(func.count()).select_from(LearningTrigger)) == 1
    )
    assert policy.baseline_dataset_id == data.id and policy.state == "waiting"


@pytest.mark.parametrize("changed_validation", [False, True])
async def test_contract_upgrade_does_not_retry_same_failed_training_content(
    db_session, super_admin, httpx_client, frozen_source, changed_validation
):
    from training_contract.dataset import DETECTION_CONTRACT

    owner, token = super_admin
    _, _, policy, _, _ = await setup_policy(db_session, httpx_client, owner, token)
    policy.baseline = semantic_partitions(manifest({"a": "label-a"}))
    old, old_data, run = await build_candidate(db_session, policy)
    assert run is not None and old.changed_samples == 1
    saved_manifest = copy.deepcopy(old_data.manifest)
    saved_digest = old.train_digest
    run.status = "failed"
    await db_session.flush()
    await service.check_policy(db_session, policy.id, force=True)
    frozen_source["revision"] += 1
    current = {
        **manifest(val="corrected" if changed_validation else "v1"),
        **DETECTION_CONTRACT,
    }
    skipped, _, retried = await build_candidate(db_session, policy, current)
    assert skipped.train_digest != saved_digest
    assert retried is None and skipped.status == "skipped"
    assert "失败" in skipped.message or "相同候选" in skipped.message
    assert old_data.manifest == saved_manifest and old.train_digest == saved_digest
