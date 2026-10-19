"""Standalone HTTP service and polling worker entrypoint."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from uuid import uuid4

from .config import ConfigurationError, load_config

logger = logging.getLogger("aap_labeling_mask_gc")


def _arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="aap-labeling-mask-gc")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one bounded collection cycle and exit",
    )
    return parser.parse_args(argv)


async def _run_cycle(data, storage, *, worker_id, scan_pages=1, delete_limit=100):
    from aap_labeling_mask_gc.coordinator import run_cycle

    try:
        return await run_cycle(
            data,
            storage,
            worker_id=worker_id,
            scan_pages=scan_pages,
            delete_limit=delete_limit,
        )
    except Exception:
        logger.warning("Mask GC cycle stopped before reporting stats")
        return None


async def _poll(data, storage, *, worker_id, run_lock, poll_seconds):
    while True:
        if not run_lock.locked():
            async with run_lock:
                await _run_cycle(data, storage, worker_id=worker_id)
        await asyncio.sleep(poll_seconds)


async def _serve(config, *, once: bool) -> None:
    import asyncpg

    from aap_assets_mask_content.gc_client import MaskGcClient
    from aap_labeling_mask_gc.coordinator import run_cycle

    from .api import create_app
    from .database import AsyncpgMaskGcData

    pool = await asyncpg.create_pool(
        dsn=config.database_url,
        min_size=1,
        max_size=2,
        timeout=5,
        command_timeout=5,
        server_settings={"application_name": "aap-labeling-mask-gc"},
    )
    data = AsyncpgMaskGcData(pool, timeout_seconds=5)
    storage = MaskGcClient(config.content_url, config.content_token)
    worker_id = uuid4()
    run_lock = asyncio.Lock()
    try:
        storage.__enter__()
        if once:
            result = await run_cycle(data, storage, worker_id=worker_id)
            sys.stdout.write(result.model_dump_json() + "\n")
            return

        import uvicorn

        app = create_app(
            data,
            storage,
            config.token,
            worker_id=worker_id,
            run_lock=run_lock,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=config.host,
                port=config.port,
                access_log=False,
                log_config=None,
            )
        )
        worker = (
            asyncio.create_task(
                _poll(
                    data,
                    storage,
                    worker_id=worker_id,
                    run_lock=run_lock,
                    poll_seconds=config.poll_seconds,
                )
            )
            if config.poll_enabled
            else None
        )
        try:
            await server.serve()
        finally:
            if worker is not None:
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
    finally:
        await asyncio.to_thread(storage.close)
        await pool.close()


def main(argv=None) -> None:
    arguments = _arguments(argv)
    try:
        config = load_config()
        asyncio.run(_serve(config, once=arguments.once))
    except ConfigurationError:
        sys.stderr.write("Mask GC runtime configuration is invalid\n")
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        return
    except Exception:
        logger.error("Mask GC runtime stopped after an unexpected failure")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
