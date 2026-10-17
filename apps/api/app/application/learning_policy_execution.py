"""Bind each policy check and its source read to the same local transaction."""

import logging

from sqlalchemy import or_, select

from app.db.models.learning_policy import LearningPolicy, LearningTrigger
from app.services import learning_policies as policies, training_runs as runs
from app.services.labeling_assets import SqlAlchemyProjectTaskSourceReader
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher

log = logging.getLogger(__name__)
CHECK_BATCH_SIZE = 100


async def tick(
    session_factory,
    *,
    dispatcher: TrainingDatasetDispatcher,
    policy_id=None,
    force=False,
):
    async with session_factory() as db:
        # Advance a durable fair ordering so a long/lost run cannot pin the first page.
        ids = (
            [policy_id]
            if policy_id
            else list(
                await db.scalars(
                    select(LearningPolicy.id)
                    .where(
                        or_(
                            LearningPolicy.enabled.is_(True)
                            & (
                                LearningPolicy.next_check_at.is_(None)
                                | (LearningPolicy.next_check_at <= runs.now())
                            ),
                            LearningPolicy.id.in_(
                                select(LearningTrigger.policy_id).where(
                                    LearningTrigger.status.in_(policies.ACTIVE_TRIGGERS)
                                )
                            ),
                        )
                    )
                    .order_by(
                        LearningPolicy.last_checked_at.nullsfirst(), LearningPolicy.id
                    )
                    .limit(CHECK_BATCH_SIZE)
                )
            )
        )
    for identity in ids:
        async with session_factory() as db:
            try:
                await policies.check_policy(
                    db,
                    identity,
                    source_reader=SqlAlchemyProjectTaskSourceReader(db),
                    force=force,
                )
                await db.commit()
                await policies.dispatch_pending(db, identity, dispatcher=dispatcher)
            except Exception:
                await db.rollback()
                log.exception("Automatic learning policy check failed: %s", identity)
