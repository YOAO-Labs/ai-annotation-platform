"""Real screenshot seed through the verified private fixture owner."""

from __future__ import annotations

import asyncio
import builtins
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
import sys
import uuid
from urllib.parse import urlencode, urlsplit

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.models.dataset import Dataset
from app.db.models.fixture_run import FixtureRun, FixtureRunResource
from tests.identity_e2e_support import fixture_routing_owner
from tests.seed_owner_support import SeedOwner, private_seed_owner

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class ScreenshotSeed:
    run_id: uuid.UUID
    cache_dir: Path
    analytics_path: Path
    sessions: async_sessionmaker
    digests: dict[str, str]
    owner: SeedOwner

    async def catalog(self, *, backend_requirements: str | None = None):
        query = {"run_id": str(self.run_id), "profile": "screenshots"}
        if backend_requirements is not None:
            query["backend_requirements"] = backend_requirements
        path = "/api/v1/__test/seed/catalog?" + urlencode(query)
        result = await self.owner._request({"method": "GET", "path": path})
        return httpx.Response(
            result["status"],
            text=result["body"],
            request=httpx.Request("GET", "http://fixture.local" + path),
        )

    async def seed_run(self, run_id: uuid.UUID) -> None:
        await _seed_owned_run(self.owner, run_id, self.cache_dir, self.analytics_path)


def _source_path(variable: str, *, directory: bool) -> Path:
    raw = os.environ.get(variable)
    if not raw or not Path(raw).is_absolute():
        raise RuntimeError(f"{variable} must name an absolute verified source path")
    path = Path(raw).resolve(strict=True)
    if not (path.is_dir() if directory else path.is_file()):
        raise RuntimeError(f"{variable} does not name the required source")
    return path


@pytest.fixture(scope="session")
def screenshot_seed_cache(tmp_path_factory):
    """Rebuild the offline inputs into a fresh pytest-owned cache."""
    from scripts.nuscenes_fixture import ensure_nuscenes_mini
    from scripts.seed_assets import _prepare_content, ensure_asset, ensure_profile
    from scripts.seed_assets import select_profile
    from scripts.seed_screenshot_assets import (
        REQUIRED_SOURCE_IDS,
        ensure_screenshot_assets,
    )

    source_cache = _source_path("AAP_SCREENSHOT_SOURCE_CACHE", directory=True)
    archive = _source_path("AAP_SCREENSHOT_NUSCENES_ARCHIVE", directory=False)
    selected = select_profile(
        "screenshots", required_ids={"rapidocr-image", *REQUIRED_SOURCE_IDS}
    )
    cache = tmp_path_factory.mktemp("owned-screenshot-cache").resolve()
    for asset in selected:
        ensure_asset(asset, cache_dir=source_cache, offline=True)
        source = source_cache / asset.id / asset.sha256
        destination = cache / asset.id / asset.sha256
        destination.mkdir(parents=True)
        copied = destination / "source.bin"
        shutil.copyfile(source / "source.bin", copied)
        with copied.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if copied.stat().st_size != asset.size_bytes or digest != asset.sha256:
            raise RuntimeError(f"{asset.id}: copied screenshot source is invalid")
        _prepare_content(asset, copied, destination / "content")
        shutil.copyfile(source / ".complete.json", destination / ".complete.json")
    assets = ensure_profile(
        "screenshots", cache_dir=cache, offline=True, assets=selected
    )
    source_files = {
        asset_id: assets[asset_id].root.joinpath(
            *assets[asset_id].asset.required_files[0].parts
        )
        for asset_id in REQUIRED_SOURCE_IDS
    }
    ensure_screenshot_assets(source_files=source_files, cache_dir=cache)
    shutil.copyfile(archive, cache / "v1.0-mini.tgz")
    ensure_nuscenes_mini(cache_dir=cache, offline=True)
    return cache


async def _seed_owned_run(
    owner: SeedOwner, run_id: uuid.UUID, cache: Path, analytics: Path
) -> None:
    from scripts.nuscenes_fixture import validate_nuscenes_mini
    from scripts.seed_assets import ensure_profile, select_profile
    from scripts.seed_screenshot_assets import (
        GENERATED_REVISION,
        REQUIRED_SOURCE_IDS,
        _source_digest,
        _validate,
    )

    if (
        not cache.is_absolute()
        or cache.resolve(strict=True) != cache
        or analytics.is_symlink()
        or Path(settings.duckdb_path) != analytics
        or os.environ.get("DUCKDB_PATH") != str(analytics)
    ):
        raise RuntimeError("screenshot seed paths are not the task-owned paths")
    selected = select_profile(
        "screenshots", required_ids={"rapidocr-image", *REQUIRED_SOURCE_IDS}
    )
    assets = ensure_profile(
        "screenshots", cache_dir=cache, offline=True, assets=selected
    )
    sources = {
        key: assets[key].root.joinpath(*assets[key].asset.required_files[0].parts)
        for key in REQUIRED_SOURCE_IDS
    }
    _validate(
        cache / "prepared" / GENERATED_REVISION, source_sha256=_source_digest(sources)
    )
    validate_nuscenes_mini(cache / "content")
    from fixture_suite_coordinator import _settled

    result = await owner._request(
        {"operation": "owner_screenshot_native_prepare", "run_id": str(run_id)}
    )
    if result["status"] != 200:
        raise RuntimeError("Screenshot native preparation failed")
    binding = json.loads(result["body"])
    if not isinstance(binding, dict) or set(binding) != {
        "admission_id", "origin", "token", "control_dsn_file"
    }:
        raise RuntimeError("Screenshot native binding is invalid")
    try:
        admission = uuid.UUID(binding["admission_id"])
        origin = urlsplit(binding["origin"])
        valid_origin = (
            origin.scheme == "http" and origin.hostname == "127.0.0.1"
            and origin.port is not None and origin.username is None and origin.password is None
            and not origin.path and not origin.query and not origin.fragment
        )
    except (TypeError, ValueError, AttributeError):
        raise RuntimeError("Screenshot native binding is invalid") from None
    if (
        str(admission) != binding["admission_id"] or not valid_origin
        or not isinstance(binding["token"], str)
        or not isinstance(binding["control_dsn_file"], str)
        or not Path(binding["control_dsn_file"]).is_absolute()
        or re.fullmatch(r"[A-Za-z0-9_-]{43}", binding["token"]) is None
    ):
        raise RuntimeError("Screenshot native binding is invalid")
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith((
            "AWS_", "MC_HOST_", "AAP_PRIVATE_FIXTURE_", "AAP_ASSETS_ADDITIONAL_ORIGINAL_",
            "ASSETS_MEDIA_S3_", "ASSETS_IMAGES_S3_", "AAP_SOURCE_FENCE_",
        ))
        and key.upper() not in {
            "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD", "AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER",
        }
    }
    environment.update(
        MINIO_ACCESS_KEY="", MINIO_SECRET_KEY="", MIGRATION_DATABASE_URL="", TEST_DATABASE_URL="",
        AWS_SHARED_CREDENTIALS_FILE="/dev/null", AWS_CONFIG_FILE="/dev/null",
        AWS_EC2_METADATA_DISABLED="true",
        ASSETS_MEDIA_URL=binding["origin"], ASSETS_MEDIA_TOKEN=binding["token"],
        AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE=binding["control_dsn_file"],
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(ROOT / "apps/api/scripts/seed.py"),
        "--profile", "screenshots", "--run-id", str(run_id),
        "--admission-id", str(admission), "--cache-dir", str(cache), "--offline",
        cwd=ROOT / "apps/api", env=environment,
    )
    status = await _settled(asyncio.create_task(process.wait()))
    if status != 0:
        raise RuntimeError(f"Screenshot seed process exited with status {status}")


@pytest.fixture
async def screenshot_seed(test_engine, monkeypatch, tmp_path, screenshot_seed_cache):
    """Create the actual offline profile and retire only this registered root."""
    if str(ROOT / "scripts") not in sys.path:
        sys.path.insert(0, str(ROOT / "scripts"))
    from fixture_suite_coordinator import verified_fixture_owner_url

    analytics = tmp_path.resolve(strict=True) / "screenshot-analytics.duckdb"
    if analytics.exists() or analytics.is_symlink():
        raise RuntimeError(
            "screenshot analytics target must be a fresh task-owned path"
        )
    with monkeypatch.context() as routing_patch:
        routing_patch.setattr(settings, "duckdb_path", str(analytics))
        routing_patch.setenv("DUCKDB_PATH", str(analytics))
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
                await _seed_owned_run(owner, run_id, screenshot_seed_cache, analytics)
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
                    run_id=run_id,
                    cache_dir=screenshot_seed_cache,
                    analytics_path=analytics,
                    sessions=sessions,
                    digests=digests,
                    owner=owner,
                )
            except BaseException as exc:
                failure = exc
                raise
            finally:
                cleanup_failure = None
                try:
                    response = await owner.post(
                        "/api/v1/__test/seed/screenshot-cleanup",
                        json={"run_id": str(run_id)},
                    )
                    assert response.status_code == 200, response.text
                    async with sessions() as db:
                        assert (
                            await db.scalar(
                                select(FixtureRun.state).where(FixtureRun.id == run_id)
                            )
                            == "retired"
                        )
                        assert not await db.scalar(
                            select(FixtureRunResource.resource_id)
                            .where(
                                FixtureRunResource.run_id == run_id,
                                FixtureRunResource.state != "retired",
                            )
                            .limit(1)
                        )
                    await owner.shutdown_authority(run_id)
                except BaseException as exc:
                    cleanup_failure = exc
                try:
                    await engine.dispose()
                except BaseException as disposal_failure:
                    errors = [
                        error
                        for error in (failure, cleanup_failure, disposal_failure)
                        if error is not None
                    ]
                    if len(errors) > 1:
                        raise builtins.BaseExceptionGroup(
                            "Screenshot operation, cleanup, or disposal failed", errors
                        ) from None
                    raise
                if cleanup_failure is not None:
                    if failure is not None:
                        raise builtins.BaseExceptionGroup(
                            "Screenshot seed and exact cleanup failed",
                            [failure, cleanup_failure],
                        ) from None
                    raise cleanup_failure
