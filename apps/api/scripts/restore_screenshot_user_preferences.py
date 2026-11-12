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
    raw_run_id = os.environ.get("SCREENSHOT_FIXTURE_RUN_ID")
    if not raw_run_id:
        raise RuntimeError("SCREENSHOT_FIXTURE_RUN_ID is required")
    run_id = UUID(raw_run_id)
    from scripts.fixture_tools import configure_fixture_tools_path

    configure_fixture_tools_path()
    from fixture_suite_coordinator import verified_fixture_owner_url

    database_url = await verified_fixture_owner_url()
    preferences = json.loads(preferences_file.read_text(encoding="utf-8"))
    if not isinstance(preferences, dict):
        raise ValueError("preferences JSON must be an object")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            owned = await connection.scalar(
                text(
                    "SELECT resource.resource_id FROM yoao_fixture_v1.fixture_run_resources AS resource "
                    "JOIN yoao_fixture_v1.fixture_runs AS run ON run.id=resource.run_id "
                    "WHERE resource.run_id=:run_id AND resource.kind='user' "
                    "AND resource.resource_id=:user_id AND resource.state='published' "
                    "AND run.family='screenshot' AND run.parent_run_id IS NULL AND run.state='open' "
                    "FOR SHARE OF run"
                ),
                {"run_id": run_id, "user_id": user_id},
            )
            if owned != user_id:
                raise RuntimeError(
                    "preference target is not owned by this screenshot run"
                )
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
