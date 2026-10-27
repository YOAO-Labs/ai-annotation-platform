"""Real pool rows, conditional object reads and verified native-shaped evidence."""

import io
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
from sqlalchemy import select

from app.db.models.active_learning import ActiveLearningAttempt as Attempt
from app.schemas.active_learning import RoundCreate, Selection
from app.services import active_learning as service, active_learning_pool as pool
from app.services.assets_active_learning_fence import (
    SqlAlchemyAssetsActiveLearningFence,
)
from app.services.labeling_active_learning_sources import (
    SqlAlchemyLabelingActiveLearningSources,
)
from app.services.training_datasets import bind_training_authority
from app.services.identity_account_lock_data import IdentityAccountLockData
from tests.factory import create_task
from tests.sample_evaluation_fixtures import Objects, prediction, seed as seed_model
from tests.sample_evaluation_original_fixtures import (
    Objects as OriginalObjects,
    registered,
)
from tests.test_original_binary import request as original_request


async def seed(db, owner, monkeypatch, *, count=6, duplicate=False):
    from tests.training_consumer_fixtures import mock_legacy_transport

    mock_legacy_transport(monkeypatch)
    objects = Objects()
    monkeypatch.setattr(service.storage, "client", objects)
    monkeypatch.setattr(service.storage, "delete_prefix", objects.delete_prefix)
    world = await seed_model(db, owner, objects)
    originals = OriginalObjects().install(monkeypatch)
    monkeypatch.setattr(
        pool, "qualification", AsyncMock(return_value=(world.ref, world.profile))
    )
    tasks, replicas, dataset_ids = [], {}, []
    for index in range(count):
        task = await create_task(db, project_id=world.project.id)
        task.total_annotations = 0
        raw = io.BytesIO()
        Image.new(
            "RGB",
            (16, 12),
            (20 + (0 if duplicate and index == 1 else index) * 20, 50, 90),
        ).save(raw, format="PNG")
        item, _asset, replica = await registered(db, task, owner, raw.getvalue())
        originals.put(replica.bucket, replica.object_key, raw.getvalue())
        replicas[task.id] = replica
        dataset_ids.append(item.dataset_id)
        tasks.append(task)
    await db.flush()
    selection = Selection(
        model_version_id=world.model.id,
        deployment_id=world.deployment.id,
        sample_task_ids=[task.id for task in tasks],
        seed=42,
        batch_size=2,
        max_inference_dispatches=12,
        max_review_tasks=4,
    )
    return SimpleNamespace(
        **vars(world),
        tasks=tasks,
        selection=selection,
        objects=objects,
        originals=originals,
        replicas=replicas,
        dataset_ids=dataset_ids,
    )


async def create(db, owner, world):
    candidate_sources = SqlAlchemyLabelingActiveLearningSources(
        db,
        dataset_bucket=service.storage.datasets_bucket,
        task_bucket=service.storage.bucket,
    )
    assets_fence = SqlAlchemyAssetsActiveLearningFence(db)
    prepared = await pool.prepare(
        db,
        world.project,
        world.selection,
        owner.id,
        request=original_request(owner),
        candidate_sources=candidate_sources,
        training_authority=bind_training_authority(db),
    )
    body = RoundCreate(
        **world.selection.model_dump(),
        request_id=uuid.uuid4(),
        name="Human selection",
        preview_digest=prepared["preview_digest"],
    )
    round = await service.create_round(
        db,
        world.project,
        owner,
        body,
        prepared,
        candidate_sources=candidate_sources,
        assets_fence=assets_fence,
        training_authority=bind_training_authority(db),
    )
    await db.flush()
    return round, await db.get(Attempt, round.current_attempt_id), body


def candidate_ports(db):
    return (
        SqlAlchemyLabelingActiveLearningSources(
            db,
            dataset_bucket=service.storage.datasets_bucket,
            task_bucket=service.storage.bucket,
        ),
        SqlAlchemyAssetsActiveLearningFence(db),
    )


async def prepare_with_ports(
    db, project, selection, actor_id, api_key_id=None, *, request=None
):
    candidate_sources, _assets_fence = candidate_ports(db)
    if request is None:
        actor = await IdentityAccountLockData(db).read_actor_status(actor_id)
        request = original_request(actor)
    return await pool.prepare(
        db,
        project,
        selection,
        actor_id,
        api_key_id,
        request=request,
        candidate_sources=candidate_sources,
        training_authority=bind_training_authority(db),
    )


async def decide_with_ports(
    db, project, user, round_id, body, api_key_id=None, *, request=None
):
    candidate_sources, assets_fence = candidate_ports(db)
    return await service.decide(
        db,
        project,
        user,
        round_id,
        body,
        api_key_id,
        request=request or original_request(user),
        candidate_sources=candidate_sources,
        assets_fence=assets_fence,
        training_authority=bind_training_authority(db),
    )


async def complete(db, round, attempt):
    rows = await service.candidate_rows(db, round)
    for row in rows:
        sample = round.spec["samples"][row.ordinal]
        await service.prepare_image(db, round, sample)
        row.media = service.media_descriptor(round, sample)
        row.request = service.managed.request_identity(
            round.spec, row, sample["preprocessing"]
        )
        row.evidence = service.successful_evidence(
            round, attempt, row, prediction(round, row)
        )
        row.status, row.produced_attempt_id = "succeeded", attempt.id
    round.inference_dispatches = len(rows)
    await db.flush()
    report = await service.build_report(db, round, attempt)
    await service.mark_terminal(db, round, attempt, "succeeded", report=report)
    await db.flush()
    return report


async def current_round(db, identity):
    from app.db.models.active_learning import ActiveLearningRound

    return await db.scalar(
        select(ActiveLearningRound)
        .where(ActiveLearningRound.id == identity)
        .execution_options(populate_existing=True)
    )
