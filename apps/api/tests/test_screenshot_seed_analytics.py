"""Screenshot bootstrap uses real DuckDB and panel queries, without PostgreSQL."""

from collections import namedtuple
from datetime import datetime, timezone
from types import SimpleNamespace
import uuid
from zoneinfo import ZoneInfo

import duckdb
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.v1.admin_analytics import router
from app.config import _REPO_ROOT_DUCKDB, Settings, settings
from app.services.screenshot_seed_analytics import (
    prepare_screenshot_analytics,
    validate_screenshot_analytics_target,
)

PANELS = (
    "throughput_daily",
    "reject_rate_by_type",
    "duration_dist",
    "activity_heatmap",
)


@pytest.fixture
def analytics_target(monkeypatch, tmp_path):
    monkeypatch.setattr(
        settings, "__pydantic_fields_set__", settings.model_fields_set.copy()
    )
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://unused/capture_test"
    )
    monkeypatch.setattr(settings, "duckdb_path", str(tmp_path / "analytics.duckdb"))
    return tmp_path / "analytics.duckdb"


class SourceSession:
    """Replace only PostgreSQL reads; keep real synchronization and panel queries."""

    def __init__(self, populated):
        now = datetime.now(timezone.utc)
        columns = (
            "id task_id user_id project_id kind started_at ended_at duration_ms "
            "annotation_count was_rejected reject_reason_type created_at"
        )
        event = namedtuple("Event", columns)(
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            "annotate",
            now,
            now,
            1234,
            1,
            True,
            "geometry",
            now,
        )
        audit_columns = (
            "id actor_id actor_role action target_type target_id status_code created_at"
        )
        audit_event = namedtuple("AuditEvent", audit_columns)(
            42,
            uuid.uuid4(),
            "admin",
            "project.create",
            "project",
            "P-ANALYTICS-42",
            201,
            now,
        )
        self.events = [event] if populated else []
        self.audit_events = [audit_event] if populated else []
        self.queries = []

    async def execute(self, statement):
        query = str(statement)
        self.queries.append(query)
        if "FROM task_events" in query:
            rows = self.events
        elif "aap_audit_v1.audit_events" in query:
            rows = self.audit_events
        else:
            rows = []
        return SimpleNamespace(all=lambda: rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("populated", [False, True])
async def test_fresh_seed_initializes_all_real_panels(analytics_target, populated):
    app = FastAPI()
    app.include_router(router, prefix="/analytics")
    auth = router.routes[0].dependant.dependencies[0].call
    app.dependency_overrides[auth] = lambda: None
    source = SourceSession(populated)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # Reproduce a fresh environment before any periodic sync has run.
        assert not analytics_target.exists()
        for panel in PANELS:
            assert (await client.get(f"/analytics/{panel}")).status_code == 503

        report = await prepare_screenshot_analytics(source)
        assert report["task_events"]["rows"] == int(populated)
        assert report["audit_logs"]["rows"] == int(populated)
        assert len(source.queries) == 2
        assert "FROM task_events" in source.queries[0]
        assert "aap_audit_v1.audit_events" in source.queries[1]
        panels = {}
        for panel in PANELS:
            response = await client.get(f"/analytics/{panel}")
            assert response.status_code == 200
            assert response.json()["panel"] == panel
            panels[panel] = response.json()["data"]
        assert panels["duration_dist"]["n"] == int(populated)
        assert len(panels["throughput_daily"]) == int(populated)
        assert len(panels["reject_rate_by_type"]) == int(populated)
        assert len(panels["activity_heatmap"]) == int(populated)
        if populated:
            assert panels["duration_dist"]["mean"] == 1234
            assert panels["reject_rate_by_type"][0]["reason_type"] == "geometry"
            with duckdb.connect(str(analytics_target), read_only=True) as con:
                synced_audit = con.execute(
                    "SELECT id, actor_id, actor_role, action, target_type, target_id, "
                    "status_code, created_at FROM audit_logs"
                ).fetchone()
                timezone_name = con.execute(
                    "SELECT current_setting('TimeZone')"
                ).fetchone()[0]
            assert synced_audit[:7] == tuple(source.audit_events[0][:7])
            # The DuckDB target is a naive TIMESTAMP, so keep its current session-
            # timezone conversion behavior while checking the source instant.
            expected_at = source.audit_events[0].created_at.astimezone(
                ZoneInfo(timezone_name)
            )
            assert synced_audit[7] == expected_at.replace(tzinfo=None)


def test_shared_development_file_is_rejected(analytics_target, monkeypatch):
    monkeypatch.setattr(settings, "duckdb_path", _REPO_ROOT_DUCKDB)
    with pytest.raises(RuntimeError, match="shared development DuckDB"):
        validate_screenshot_analytics_target()


def test_non_test_database_is_rejected(analytics_target, monkeypatch):
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://unused/annotation"
    )
    with pytest.raises(RuntimeError, match="isolated"):
        validate_screenshot_analytics_target()


def test_test_store_default_is_stable_and_server_specific(monkeypatch):
    monkeypatch.delenv("DUCKDB_PATH", raising=False)
    first = Settings(
        _env_file=None, database_url="postgresql+asyncpg://one:5432/capture_test"
    )
    repeat = Settings(
        _env_file=None, database_url="postgresql+asyncpg://one:5432/capture_test"
    )
    other = Settings(
        _env_file=None, database_url="postgresql+asyncpg://two:5432/capture_test"
    )
    assert first.duckdb_path == repeat.duckdb_path
    assert first.duckdb_path != other.duckdb_path
    assert "/.artifacts/analytics/" in first.duckdb_path
    assert first.duckdb_path != _REPO_ROOT_DUCKDB
    dev = Settings(
        _env_file=None, database_url="postgresql+asyncpg://one:5432/annotation"
    )
    assert dev.duckdb_path == _REPO_ROOT_DUCKDB
    explicit = Settings(
        _env_file=None,
        database_url=first.database_url,
        duckdb_path="/private/analytics.duckdb",
    )
    assert explicit.duckdb_path == "/private/analytics.duckdb"


def test_test_store_separates_databases_and_checkouts(monkeypatch, tmp_path):
    from app import config

    monkeypatch.delenv("DUCKDB_PATH", raising=False)
    first = Settings(
        _env_file=None, database_url="postgresql+asyncpg://one:5432/first_test"
    )
    second = Settings(
        _env_file=None, database_url="postgresql+asyncpg://one:5432/second_e2e"
    )
    assert first.duckdb_path != second.duckdb_path
    credential_change = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:secret@one:5432/first_test",
    )
    assert credential_change.duckdb_path == first.duckdb_path
    monkeypatch.setattr(config, "_PARENTS", [tmp_path] * 4)
    other_checkout = Settings(_env_file=None, database_url=first.database_url)
    assert other_checkout.duckdb_path != first.duckdb_path
    assert other_checkout.duckdb_path.startswith(str(tmp_path))


@pytest.mark.asyncio
async def test_seed_rejects_shared_target_before_fixture_or_database_writes(
    analytics_target, monkeypatch
):
    from scripts import seed

    monkeypatch.setattr(settings, "duckdb_path", _REPO_ROOT_DUCKDB)

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe target reached fixture preparation or database session")

    monkeypatch.setattr(seed, "ensure_profile", forbidden)
    monkeypatch.setattr(seed, "Session", forbidden)
    with pytest.raises(RuntimeError, match="shared development DuckDB"):
        await seed.seed(profile="screenshots")


@pytest.mark.asyncio
async def test_screenshot_seed_bootstraps_analytics_before_returning(
    analytics_target, monkeypatch, tmp_path
):
    from contextlib import asynccontextmanager
    from pathlib import PurePosixPath
    from unittest.mock import AsyncMock

    from scripts import seed

    source = SourceSession(False)
    source.commit = AsyncMock()

    @asynccontextmanager
    async def session():
        yield source

    asset = SimpleNamespace(
        root=tmp_path,
        asset=SimpleNamespace(
            required_files=[PurePosixPath("source.jpg")], sha256="fixture"
        ),
    )
    monkeypatch.setattr(seed, "Session", session)
    monkeypatch.setattr(seed, "engine", SimpleNamespace(dispose=AsyncMock()))
    monkeypatch.setattr(seed, "USERS", [])
    monkeypatch.setattr(seed, "select_profile", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        seed,
        "ensure_profile",
        lambda *args, **kwargs: dict.fromkeys(
            ["rapidocr-image", *seed.REQUIRED_SOURCE_IDS], asset
        ),
    )
    monkeypatch.setattr(
        seed,
        "ensure_screenshot_assets",
        lambda **kwargs: SimpleNamespace(content_sha256="fixture"),
    )
    results = {
        "prepare_screenshot_seed": SimpleNamespace(
            purged_projects=0, purged_datasets=0
        ),
        "_ensure_demo_memberships": 0,
        "reconcile_screenshot_seed": {"projects": 0, "tasks": 0, "batches": 0},
        "prepare_screenshot_seed_media": {"datasets": 0, "items": 0},
        "reconcile_screenshot_backends": {"bindings": {}},
        "finalize_screenshot_seed_timestamps": None,
        "build_screenshot_seed_catalog": {},
    }
    for name, result in results.items():
        monkeypatch.setattr(seed, name, AsyncMock(return_value=result))

    await seed.seed(profile="screenshots", backend_requirements="none")
    assert analytics_target.exists()
    assert len(source.queries) == 2
