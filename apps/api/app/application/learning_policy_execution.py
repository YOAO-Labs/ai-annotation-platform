"""Bind each policy check and its source read to the same local transaction."""

import logging
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import learning_selection_data as data
from app.services import learning_policies as policies, training_runs as runs
from app.services.training_dataset_dispatch import TrainingDatasetDispatcher
from app.services.training_authority_contract import TrainingAuthorityPort

log = logging.getLogger(__name__)


async def tick(
    session_factory,
    *,
    dispatcher: TrainingDatasetDispatcher,
    training_authority_factory: Callable[[AsyncSession], TrainingAuthorityPort],
    policy_id=None,
    force=False,
):
    async with session_factory() as db:
        ids = [policy_id] if policy_id else await data.due_policies(db, runs.now())
    for identity in ids:
        async with session_factory() as db:
            try:
                await policies.check_policy(
                    db,
                    identity,
                    training_authority=training_authority_factory(db),
                    force=force,
                )
                await db.commit()
                await policies.dispatch_pending(db, identity, dispatcher=dispatcher)
            except Exception:
                await db.rollback()
                log.exception("Automatic learning policy check failed: %s", identity)
