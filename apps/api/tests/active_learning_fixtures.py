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
from tests.factory import create_task
from tests.sample_evaluation_fixtures import Objects, prediction, seed as seed_model


async def seed(db, owner, monkeypatch, *, count=6, duplicate=False):
    objects = Objects()
    monkeypatch.setattr(service.storage, "client", objects)
    monkeypatch.setattr(service.storage, "delete_prefix", objects.delete_prefix)
    world = await seed_model(db, owner, objects)
    monkeypatch.setattr(
        pool, "qualification", AsyncMock(return_value=(world.ref, world.profile))
    )
    tasks = []
    for index in range(count):
        task = await create_task(db, project_id=world.project.id)
        task.total_annotations = 0
        raw = io.BytesIO()
        Image.new(
            "RGB",
            (16, 12),
            (20 + (0 if duplicate and index == 1 else index) * 20, 50, 90),
        ).save(raw, format="PNG")
        objects.put_object(
            Bucket=service.storage.bucket, Key=task.file_path, Body=raw.getvalue()
        )
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
        **vars(world), tasks=tasks, selection=selection, objects=objects
    )


async def create(db, owner, world):
    prepared = await pool.prepare(db, world.project, world.selection, owner.id)
    body = RoundCreate(
        **world.selection.model_dump(),
        request_id=uuid.uuid4(),
        name="Human selection",
        preview_digest=prepared["preview_digest"],
    )
    round = await service.create_round(db, world.project, owner, body, prepared)
    await db.flush()
    return round, await db.get(Attempt, round.current_attempt_id), body


async def complete(db, round, attempt):
    rows = await service.candidate_rows(db, round)
    for row in rows:
        sample = round.spec["samples"][row.ordinal]
        service.prepare_image(round, sample)
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
