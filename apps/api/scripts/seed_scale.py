"""Create and remove a scale fixture owned by an explicit isolated run.

Usage: python scripts/seed_scale.py --run-id UUID [N [M [K]]]
       python scripts/seed_scale.py --run-id UUID --purge

The host coordinator must register the run before this command starts. The run
ledger owns the exact project and dataset IDs. An interrupted admission remains
a blocker; it is never treated as proof that the creator stopped.
"""

import argparse
import builtins
import asyncio
import os
import sys
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

if settings.environment == "production":
    print("[seed_scale] refusing to run with environment=production", file=sys.stderr)
    raise SystemExit(2)

from app.db.models.fixture_run import FixtureRun  # noqa: E402
from app.services import fixture_run_ownership as ownership  # noqa: E402
from app.services.labeling_scale_seed_operations import LabelingScaleSeedOperations  # noqa: E402
from app.services.test_seed_access import assert_isolated_test_seed_database  # noqa: E402
from app.services.workspace_scale_seed_data import (  # noqa: E402
    create_scale_seed_project,
    ensure_scale_seed_member,
    prepare_scale_seed_cleanup,
)

engine = create_async_engine(settings.database_url, echo=False)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _marker(run_id: uuid.UUID) -> str:
    return f"scale-seed-{run_id.hex}"


async def _purge(db: AsyncSession, run_id: uuid.UUID) -> None:
    from app.api.v1._test_seed import _OwnedFixtureScope, _cleanup_owned_e2e_fixtures

    scope = _OwnedFixtureScope(namespace=run_id.hex[:12], run_id=run_id)
    await _cleanup_owned_e2e_fixtures(db, scope, family="scale")


async def seed(
    n_tasks: int,
    n_batches: int,
    n_annotated: int,
    *,
    run_id: uuid.UUID,
    owner_sessions: async_sessionmaker[AsyncSession],
    selector_sessions: async_sessionmaker[AsyncSession],
) -> None:
    from app.services.identity_commands import IdentityCommandService

    marker = _marker(run_id)
    async with owner_sessions() as db:
        await assert_isolated_test_seed_database(db)
        run = await db.get(FixtureRun, run_id)
        if run is None or run.family != "scale" or run.parent_run_id is not None:
            raise HTTPException(409, detail={"code": "fixture_run_not_prepared"})
        admission_id = await ownership.admit(db, run_id, "scale_seed")
        await db.commit()
        try:
            projects, datasets = await prepare_scale_seed_cleanup(db, marker)
            if projects or datasets:
                raise HTTPException(409, detail={"code": "fixture_run_scope_changed"})
            labeling = LabelingScaleSeedOperations(db)
            async with selector_sessions() as selector_db:
                owner_id, annotator_id = await IdentityCommandService(
                    selector_db
                ).resolve_scale_seed_actors()
            if not owner_id or not annotator_id:
                print("[seed_scale] need a super_admin and an annotator project member")
                raise SystemExit(1)

            project_id = await create_scale_seed_project(
                db, owner_id=owner_id, marker=marker, total_tasks=n_tasks
            )
            await ownership.reserve(
                db, run_id, "project", project_id, logical_key="project", published=True
            )
            await ensure_scale_seed_member(
                db, project_id=project_id, annotator_id=annotator_id, owner_id=owner_id
            )
            dataset_id = (
                await db.execute(
                    text(
                        "INSERT INTO datasets (id, display_id, name, created_by) VALUES (gen_random_uuid(), 'D-'||nextval('display_seq_datasets'), :m, :owner) RETURNING id"
                    ),
                    {"m": marker, "owner": owner_id},
                )
            ).scalar_one()
            await ownership.reserve(
                db, run_id, "dataset", dataset_id, logical_key="dataset", published=True
            )
            await db.execute(
                text(
                    "INSERT INTO dataset_items (id, dataset_id, file_name, file_path) SELECT gen_random_uuid(), :ds, 'img_'||g||'.jpg', '/seed/'||g||'.jpg' FROM generate_series(1, :n) g"
                ),
                {"ds": dataset_id, "n": n_tasks},
            )
            await labeling.create_batches(
                marker, project_id, dataset_id, n_batches, annotator_id
            )
            await db.commit()
            await labeling.create_tasks(marker, project_id, dataset_id)
            await db.commit()
            await labeling.create_annotations(
                marker, project_id, annotator_id, n_annotated
            )
            await db.commit()
            batch_id = await labeling.read_first_batch_id(project_id)
        except BaseException as failure:
            try:
                await db.rollback()
                await ownership.settle(db, run_id, admission_id)
                await db.commit()
            except BaseException as settlement_error:
                raise builtins.BaseExceptionGroup(
                    "Scale seed failed and admission settlement failed",
                    [failure, settlement_error],
                ) from None
            raise
        else:
            await ownership.settle(db, run_id, admission_id)
            await db.commit()
            print(
                f"[seed_scale] done: {n_tasks} tasks / {n_batches} batches / {n_annotated} annotated"
            )
            print(f"  run_id       = {run_id}")
            print(f"  project_id   = {project_id}")
            print(f"  a_batch_id   = {batch_id}")
            print(f"  annotator_id = {annotator_id}")
            print(
                f"  EXPLAIN ANALYZE SELECT * FROM aap_labeling_v1.task_rows WHERE project_id='{project_id}' ORDER BY created_at, id LIMIT 100;"
            )


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=uuid.UUID)
    parser.add_argument("--purge", action="store_true")
    parser.add_argument("n", nargs="?", type=int, default=100_000)
    parser.add_argument("m", nargs="?", type=int, default=50)
    parser.add_argument("k", nargs="?", type=int, default=20_000)
    args = parser.parse_args()
    selector_sessions = Session
    owner_engine = None
    try:
        from scripts.fixture_tools import configure_fixture_tools_path

        configure_fixture_tools_path()
        from fixture_suite_coordinator import verified_fixture_owner_url

        runtime_url = make_url(os.environ["DATABASE_URL"])
        fixture_runtime = os.environ.get("FIXTURE_RUNTIME_DATABASE_URL")
        if engine.url != runtime_url or (
            fixture_runtime and engine.url != make_url(fixture_runtime)
        ):
            raise RuntimeError(
                "Selector database differs from the verified fixture runtime"
            )

        async with Session() as db:
            await assert_isolated_test_seed_database(db)

        owner_engine = create_async_engine(
            await verified_fixture_owner_url(), echo=False, hide_parameters=True
        )
        owner_sessions = async_sessionmaker(
            owner_engine, class_=AsyncSession, expire_on_commit=False
        )
        if args.purge:
            async with owner_sessions() as db:
                await _purge(db, args.run_id)
            print(f"[seed_scale] purged run {args.run_id}")
        else:
            await seed(
                args.n,
                args.m,
                args.k,
                run_id=args.run_id,
                owner_sessions=owner_sessions,
                selector_sessions=selector_sessions,
            )
    finally:
        if owner_engine is not None:
            await owner_engine.dispose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
