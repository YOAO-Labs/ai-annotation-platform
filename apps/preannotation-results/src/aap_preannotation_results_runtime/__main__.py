"""Execute one allowlisted native result operation from stdin."""

from __future__ import annotations

import argparse
import asyncio
import sys

from aap_preannotation_results import contract as c
from pydantic import BaseModel

from .config import ConfigurationError, load_config
from .database import AsyncpgBatchResultDatabase, EXECUTOR_OPERATIONS


def _arguments(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="aap-preannotation-results")
    parser.add_argument("operation", choices=sorted(EXECUTOR_OPERATIONS))
    return parser.parse_args(argv)


async def _invoke(config, operation: str, request: BaseModel):
    import asyncpg

    pool = await asyncpg.create_pool(
        dsn=config.database_url,
        min_size=1,
        max_size=1,
        timeout=5,
        command_timeout=5,
        server_settings={"application_name": "aap-preannotation-results"},
    )
    try:
        database = AsyncpgBatchResultDatabase(pool)
        return await database.call(operation, request)
    finally:
        await pool.close()


def _write(value) -> int:
    raw = c.json_bytes(value)
    if len(raw) > c.MAX_RESPONSE_BYTES:
        raw = c.json_bytes(c.error_envelope("invalid_response"))
        status = 1
    else:
        status = 0
    sys.stdout.buffer.write(raw + b"\n")
    return status


def main(argv=None) -> None:
    arguments = _arguments(argv)
    raw = sys.stdin.buffer.read(c.MAX_REQUEST_BYTES + 1)
    if len(raw) > c.MAX_REQUEST_BYTES:
        _write(c.error_envelope("invalid_request"))
        raise SystemExit(2)
    try:
        document = c.parse_json(raw)
        request_type = c.OPERATION[arguments.operation].request
        request = request_type.model_validate(document)
    except (KeyError, ValueError, TypeError, RecursionError, UnicodeError):
        _write(c.error_envelope("invalid_request"))
        raise SystemExit(2) from None

    try:
        config = load_config()
        result = asyncio.run(_invoke(config, arguments.operation, request))
        status = _write(c.success_envelope(result))
    except ConfigurationError:
        status = _write(c.error_envelope("storage_unavailable"))
    except c.BatchResultFailure as exc:
        status = _write(c.error_envelope(exc.code))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        status = _write(c.error_envelope("storage_unavailable"))
    if status:
        raise SystemExit(status)


if __name__ == "__main__":
    main()
