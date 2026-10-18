"""Run or health-check the independent Dataset import worker."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from contextlib import suppress

from aap_assets_source.import_contract import ImportFailure, ImportState

from .config import load_config
from .database import ImportDatabase
from .storage import DestinationStorage
from .workflow import (
    ImportWorkflow,
    LeaseUnavailable,
    PersistenceBusy,
    WorkerStopping,
    claim_next,
)

logger = logging.getLogger("aap_assets_source_import")
_NO_FINISH_CODES = {
    "account_busy",
    "dataset_busy",
    "import_cancelled",
    "job_missing",
    "job_terminal",
    "lease_lost",
}


async def _run(*, healthcheck: bool, once: bool = False) -> None:
    config = load_config()
    database = await ImportDatabase.connect(config)
    try:
        if healthcheck:
            if not await database.ready():
                raise RuntimeError("import database is not ready")
            return

        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(signum, shutdown.set)
        workflow = ImportWorkflow(
            database,
            DestinationStorage(config),
            config,
            shutdown=shutdown,
        )
        logger.info("Dataset import worker ready")
        while not shutdown.is_set():
            try:
                state = await claim_next(database, config)
            except Exception as exc:
                logger.warning(
                    "Dataset import claim failed kind=%s", type(exc).__name__
                )
                state = None
            if state is None:
                if once:
                    return
                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=config.poll_seconds)
                except TimeoutError:
                    pass
                continue
            try:
                await workflow.run_claimed(state)
            except WorkerStopping:
                return
            except (LeaseUnavailable, PersistenceBusy):
                logger.info(
                    "Dataset import lease will be recovered job=%s", state.job_id
                )
            except ImportFailure as exc:
                await _handle_failure(database, state, exc)
            except Exception as exc:
                # This can include an uncertain transaction commit; leave the lease and
                # cursor untouched so a later worker can retry the same publication.
                logger.warning(
                    "Dataset import stopped job=%s kind=%s",
                    state.job_id,
                    type(exc).__name__,
                )
            if once:
                return
    finally:
        await database.close()


async def _handle_failure(
    database: ImportDatabase, state: ImportState, failure: ImportFailure
) -> None:
    code = failure.code
    if code in _NO_FINISH_CODES:
        logger.info("Dataset import lease ended job=%s code=%s", state.job_id, code)
        return
    if state.lease_token is None:
        return
    try:
        async with database.transaction() as data:
            finished = await data.finish(state.job_id, state.lease_token, code)
        logger.warning(
            "Dataset import failed job=%s code=%s stage=%s",
            state.job_id,
            code,
            finished.stage,
        )
    except ImportFailure as finish_failure:
        logger.info(
            "Dataset import failure settlement deferred job=%s code=%s",
            state.job_id,
            finish_failure.code,
        )
    except Exception as exc:
        logger.warning(
            "Dataset import failure settlement unavailable job=%s kind=%s",
            state.job_id,
            type(exc).__name__,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Independent Dataset import worker")
    parser.add_argument(
        "--healthcheck",
        action="store_true",
        help="verify database readiness and the exact LOGIN capability scope, then exit",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="claim at most one job and exit; primarily useful for local validation",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run(healthcheck=args.healthcheck, once=args.once))


if __name__ == "__main__":
    main()
