"""AsyncPG provider with one short transaction per dispatch capability call."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .dispatch_contract import JOBS_DISPATCH_FUNCTIONS
from .dispatch_data import JobsDispatchDataClient

_JSON_FUNCTIONS = frozenset(
    JOBS_DISPATCH_FUNCTIONS[name] for name in ("prepare", "claim", "finish")
)
_STATUS_FUNCTION = JOBS_DISPATCH_FUNCTIONS["status"]


class JobsDispatchPostgres:
    """A narrow asyncpg pool that exposes the portable Jobs dispatch client."""

    def __init__(self, pool: Any):
        self._pool = pool
        self.client = JobsDispatchDataClient(self.execute)

    @classmethod
    async def connect(cls, database_url: str) -> JobsDispatchPostgres:
        if not isinstance(database_url, str) or not database_url:
            raise ValueError("Jobs dispatch database URL is required")
        try:
            from asyncpg import create_pool
        except ImportError:
            raise RuntimeError(
                "Jobs dispatch PostgreSQL support requires the asyncpg extra"
            ) from None
        pool = await create_pool(dsn=database_url, min_size=1, max_size=4)
        return cls(pool)

    async def execute(self, function_name: str, arguments: Mapping[str, Any]) -> Any:
        if function_name in _JSON_FUNCTIONS:
            if (
                set(arguments) != {"p_request"}
                or type(arguments["p_request"]) is not dict
            ):
                raise ValueError("Jobs dispatch function arguments are invalid")
            request = json.dumps(
                arguments["p_request"], allow_nan=False, separators=(",", ":")
            )
            query = f"SELECT {function_name}($1::jsonb)"
            values = (request,)
        elif function_name == _STATUS_FUNCTION:
            if set(arguments) != {"intent_id"}:
                raise ValueError("Jobs dispatch function arguments are invalid")
            raw_intent_id = arguments["intent_id"]
            if isinstance(raw_intent_id, UUID):
                intent_id = raw_intent_id
            elif type(raw_intent_id) is str:
                try:
                    intent_id = UUID(raw_intent_id)
                except ValueError:
                    raise ValueError(
                        "Jobs dispatch function arguments are invalid"
                    ) from None
            else:
                raise ValueError("Jobs dispatch function arguments are invalid")
            query = f"SELECT {function_name}($1::uuid)"
            values = (intent_id,)
        else:
            raise ValueError(
                "Jobs dispatch function is not in the fixed capability set"
            )
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                return await connection.fetchval(query, *values)

    async def close(self) -> None:
        await self._pool.close()

    async def __aenter__(self) -> JobsDispatchPostgres:
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()
