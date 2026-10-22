"""Call only the real account and membership helpers from the installed API image."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid


def _emit(value: dict[str, object]) -> None:
    print("AAP_SEED_PARENT_RESULT=" + json.dumps(value, sort_keys=True, default=str))


async def _run(operation: str, arguments: list[str]) -> None:
    from sqlalchemy import event, text

    from scripts import seed

    if operation == "accounts":
        commits = [0]

        def track_commit(_session) -> None:
            commits[0] += 1

        async with seed.Session() as db:
            event.listen(db.sync_session, "before_commit", track_commit)
            try:
                account_ids = await seed._ensure_seed_accounts(db)
            finally:
                event.remove(db.sync_session, "before_commit", track_commit)
        await seed.engine.dispose()
        _emit(
            {
                "operation": operation,
                "account_ids": {
                    email: str(account_id) for email, account_id in account_ids.items()
                },
                "commit_attempts": commits[0],
            }
        )
        return

    if operation not in {
        "memberships",
        "memberships_pending",
        "memberships_fail",
        "memberships_sleep",
    }:
        raise SystemExit("unsupported seed parent operation")
    if len(arguments) < 1:
        raise SystemExit("membership operation requires the PM account UUID")

    project_admin_id = uuid.UUID(arguments[0])
    pending_display_id = arguments[1] if len(arguments) >= 2 else None
    application_name = arguments[2] if len(arguments) >= 3 else None
    commits = [0]
    pending_dataset = None
    result: dict[str, object] = {"operation": operation}
    failure: Exception | None = None

    def track_commit(_session) -> None:
        commits[0] += 1

    try:
        async with seed.Session() as db:
            event.listen(db.sync_session, "before_commit", track_commit)
            try:
                if application_name is not None:
                    await db.execute(
                        text(
                            "SELECT pg_catalog.set_config('application_name', :tag, true)"
                        ),
                        {"tag": application_name},
                    )
                if pending_display_id is not None:
                    from app.db.models.dataset import Dataset

                    pending_dataset = Dataset(
                        display_id=pending_display_id,
                        name="pending seed parent transaction fixture",
                        created_by=project_admin_id,
                    )
                    db.add(pending_dataset)
                result["changed"] = await seed._ensure_demo_memberships(db)
                result["pending_id_materialized"] = (
                    pending_dataset is not None and pending_dataset.id is not None
                )
                if pending_dataset is not None:
                    result["pending_id"] = str(pending_dataset.id)
            finally:
                event.remove(db.sync_session, "before_commit", track_commit)
    except Exception as exc:  # the API context manager closes and rolls back first
        failure = exc
        database_error = getattr(exc, "orig", None)
        result["error_type"] = type(database_error or exc).__name__
        result["error"] = str(database_error or exc)[:500]
        result["pending_id_materialized"] = (
            pending_dataset is not None and pending_dataset.id is not None
        )
        if pending_dataset is not None and pending_dataset.id is not None:
            result["pending_id"] = str(pending_dataset.id)
    finally:
        await seed.engine.dispose()

    result["commit_attempts"] = commits[0]
    _emit(result)
    if failure is not None:
        raise failure


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: seed_parent_operator_probe.py OP [PM_UUID [PENDING_DATASET [APP_NAME]]]"
        )
    asyncio.run(_run(sys.argv[1], sys.argv[2:]))


if __name__ == "__main__":
    main()
