"""Real screenshot seed through the verified private fixture owner."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from pathlib import Path
import sys
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.models.dataset import Dataset
from app.db.models.fixture_run import FixtureRun, FixtureRunResource
from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owner_support import SeedOwner, private_seed_owner

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ScreenshotSeed:
    run_id: uuid.UUID
    sessions: async_sessionmaker
    digests: dict[str, str]
    owner: SeedOwner


@pytest.fixture
async def screenshot_seed(test_engine, monkeypatch):
    """Create the actual offline profile and retire only this registered root."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from fixture_suite_coordinator import verified_fixture_owner_url
    from scripts.seed import seed

    with monkeypatch.context() as routing_patch:
        async with (
            fixture_routing_owner(test_engine, routing_patch),
            private_seed_owner() as owner,
        ):
            owner_url = await verified_fixture_owner_url()
            parsed = make_url(owner_url)
            assert (parsed.host, parsed.port or 5432, parsed.database) == (
                test_engine.url.host,
                test_engine.url.port or 5432,
                test_engine.url.database,
            )
            run_id = uuid.uuid4()
            await owner.prepare_suite(run_id, "screenshot")
            engine = create_async_engine(owner_url, hide_parameters=True)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            failure = None
            try:
                await seed(profile="screenshots", run_id=run_id, offline=True)
                async with sessions() as db:
                    rows = (
                        await db.execute(
                            select(FixtureRunResource.logical_key, Dataset.metadata_)
                            .join(Dataset, Dataset.id == FixtureRunResource.resource_id)
                            .where(
                                FixtureRunResource.run_id == run_id,
                                FixtureRunResource.kind == "dataset",
                                FixtureRunResource.state == "published",
                            )
                        )
                    ).all()
                    digests = {
                        key: metadata["seed"]["asset_sha256"] for key, metadata in rows
                    }
                yield ScreenshotSeed(
                    run_id=run_id, sessions=sessions, digests=digests, owner=owner
                )
            except BaseException as exc:
                failure = exc
                raise
            finally:
                try:
                    response = await owner.post(
                        "/api/v1/__test/seed/screenshot-cleanup",
                        json={"run_id": str(run_id)},
                    )
                    assert response.status_code == 200, response.text
                    async with sessions() as db:
                        assert await db.scalar(
                            select(FixtureRun.state).where(FixtureRun.id == run_id)
                        ) == "retired"
                        assert not await db.scalar(
                            select(FixtureRunResource.resource_id)
                            .where(
                                FixtureRunResource.run_id == run_id,
                                FixtureRunResource.state != "retired",
                            )
                            .limit(1)
                        )
                    await owner.shutdown_authority(run_id)
                except BaseException as cleanup_failure:
                    if failure is not None:
                        raise builtins.BaseExceptionGroup(
                            "Screenshot seed and exact cleanup failed",
                            [failure, cleanup_failure],
                        ) from None
                    raise
                finally:
                    await engine.dispose()
