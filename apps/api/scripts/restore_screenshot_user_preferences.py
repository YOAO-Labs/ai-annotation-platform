"""Restore one screenshot user's JSON preferences in an isolated test database.

The public preferences endpoint intentionally deep-merges workspace context maps,
so it cannot remove a context that a recording added.  The screenshot recorder
uses this helper only after the browser has left the workbench, then writes the
captured JSON back through the public endpoint for normal validation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True, type=UUID)
    parser.add_argument("--preferences-file", required=True, type=Path)
    return parser.parse_args()


async def restore(user_id: UUID, preferences_file: Path) -> None:
    database_url = os.environ.get("SCREENSHOT_DATABASE_URL") or os.environ.get(
        "DATABASE_URL"
    )
    if not database_url:
        raise RuntimeError("SCREENSHOT_DATABASE_URL or DATABASE_URL is required")
    database_name = database_url.split("/")[-1].split("?", 1)[0]
    if not (database_name.endswith("_test") or database_name.endswith("_e2e")):
        raise RuntimeError(
            "preference restore requires an isolated *_test or *_e2e database"
        )
    preferences = json.loads(preferences_file.read_text(encoding="utf-8"))
    if not isinstance(preferences, dict):
        raise ValueError("preferences JSON must be an object")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            updated = await connection.scalar(
                text(
                    "SELECT aap_identity_v1.replace_fixture_preferences("
                    ":user_id, CAST(:preferences AS jsonb))"
                ),
                {
                    "preferences": json.dumps(preferences, ensure_ascii=False),
                    "user_id": user_id,
                },
            )
            if updated != 1:
                raise RuntimeError(f"expected one user row, updated {updated}")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(restore(arguments.user_id, arguments.preferences_file))
