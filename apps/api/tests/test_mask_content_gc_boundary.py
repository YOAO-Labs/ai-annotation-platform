"""The application releases business transactions and uses only the GC owner."""

import asyncio
from threading import Event

import httpx
import pytest

from aap_assets_mask_content.gc_contract import MaskGcFailure
from aap_labeling_mask_gc import contract as c
from aap_labeling_mask_gc.client import LabelingMaskGcClient
from app.application import raster_mask_retention as retention
from app.config import settings
from app.services import mask_gc_collection as collection
from app.services.mask_gc import IndependentMaskGcDatabase
from app.services.raster_mask_contract import RasterMaskContractError


def _select_remote(monkeypatch, transport):
    monkeypatch.setattr(settings, "labeling_mask_gc_url", "http://gc.test")
    monkeypatch.setattr(settings, "labeling_mask_gc_token", "g" * 32)
    monkeypatch.setattr(
        collection,
        "LabelingMaskGcClient",
        lambda *args, **kwargs: LabelingMaskGcClient(
            *args, **kwargs, client=httpx.Client(transport=transport)
        ),
    )

    def forbidden(*args, **kwargs):
        pytest.fail("remote collection attempted local DB or byte access")

    monkeypatch.setattr(collection, "mask_gc_storage_owner", forbidden)
    return forbidden


async def test_collection_forwards_preview_and_keeps_owner_observations(monkeypatch):
    seen = []

    def handler(request):
        seen.append(c.parse_json(request.content))
        assert request.url == "http://gc.test/v1/mask-gc/run"
        assert request.headers["Authorization"] == "Bearer " + "g" * 32
        return httpx.Response(
            200,
            json={
                "schema": c.SCHEMA,
                "ok": True,
                "data": c.CycleStats(
                    dry_run=True,
                    scan_pages=1,
                    scanned=3,
                    skipped=2,
                    eligible=1,
                    referenced=17,
                    next_after_key="raster-masks/sha256/a",
                ).model_dump(mode="json"),
            },
        )

    forbidden = _select_remote(monkeypatch, httpx.MockTransport(handler))
    request = c.RunRequest(dry_run=True, scan_pages=2, delete_limit=4)
    result = await collection.collect_raster_masks(forbidden, request)
    assert seen == [request.model_dump(mode="json")]
    assert result.referenced == 17
    assert result.eligible == 1 and result.staged == 0
    assert result.next_after_key == "raster-masks/sha256/a"


@pytest.mark.parametrize("failure", ["http", "malformed", "partial_config"])
async def test_collection_owner_failure_never_falls_back(monkeypatch, failure):
    def handler(_request):
        if failure == "http":
            raise httpx.ConnectError("secret provider endpoint")
        return httpx.Response(200, text="secret provider response")

    forbidden = _select_remote(monkeypatch, httpx.MockTransport(handler))
    if failure == "partial_config":
        monkeypatch.setattr(settings, "labeling_mask_gc_url", "")
    with pytest.raises(RasterMaskContractError) as caught:
        await collection.collect_raster_masks(forbidden, c.RunRequest())
    assert caught.value.status_code == 503
    assert caught.value.detail["reason"] == "mask_gc_unavailable"
    assert "secret" not in str(caught.value)


async def test_cancelled_remote_call_keeps_transport_alive_until_inflight_finishes(
    monkeypatch,
):
    entered, release, closed = Event(), Event(), Event()

    class Owner:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.set()

        def run(self, request):
            entered.set()
            assert release.wait(5)
            assert not closed.is_set()
            return c.CycleStats(dry_run=request.dry_run)

    monkeypatch.setattr(settings, "labeling_mask_gc_url", "http://gc.test")
    monkeypatch.setattr(collection, "LabelingMaskGcClient", Owner)
    task = asyncio.create_task(collection.collect_raster_masks(None, c.RunRequest()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()


def test_partial_remote_byte_configuration_does_not_open_local_s3(monkeypatch):
    monkeypatch.setattr(settings, "assets_mask_content_url", "http://content.test")
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")
    monkeypatch.setattr(settings, "assets_mask_cleanup_token", "")

    class NoLocalStorage:
        @property
        def client(self):
            pytest.fail("partial remote configuration accessed local S3")

    with pytest.raises(MaskGcFailure):
        with collection.mask_gc_storage_owner(NoLocalStorage()):
            pytest.fail("invalid cleanup owner was accepted")


async def test_native_collector_opens_a_fresh_session_for_each_operation(monkeypatch):
    events = []

    class Session:
        async def __aenter__(self):
            events.append(("open", self))
            return self

        async def __aexit__(self, *args):
            events.append(("close", self))

    class Operation:
        def __init__(self, db, *, commit):
            assert commit is True
            self.db = db

        async def call(self, operation, request):
            assert operation == "read_mask_gc_progress"
            return c.ReadMaskGcProgressResult(
                active_intents=0,
                indeterminate_intents=0,
                completed_intents=0,
                sweep=None,
            )

    from app.services import mask_gc

    monkeypatch.setattr(mask_gc, "SqlAlchemyMaskGcDatabase", Operation)
    owner = IndependentMaskGcDatabase(Session)
    for _ in range(2):
        await owner.call("read_mask_gc_progress", c.ReadMaskGcProgressRequest())
    assert [event for event, _ in events] == ["open", "close", "open", "close"]
    assert events[0][1] is not events[2][1]


async def test_retention_commits_expiry_and_releases_metrics_before_rpc(monkeypatch):
    events = []

    class Database:
        active = True

        async def commit(self):
            events.append("commit_expiry")
            self.active = False

        async def rollback(self):
            events.append("release_metrics")
            self.active = False

    db = Database()

    async def expire(database):
        assert database is db and db.active
        events.append("expire")
        return 2

    async def metrics(database):
        assert database is db and not db.active
        events.append("metrics")
        db.active = True

    factory = object()

    async def collect(session_factory, request):
        assert session_factory is factory and not db.active
        assert request == c.RunRequest(dry_run=True, scan_pages=3, delete_limit=7)
        events.append("rpc")
        return c.CycleStats(dry_run=True, scan_errors=1, referenced=None)

    monkeypatch.setattr(retention.ai_mask_accept, "expire_accept_decisions", expire)
    monkeypatch.setattr(retention, "expire_stale_tracker_candidates", expire)
    monkeypatch.setattr(
        retention.annotation_conversion, "expire_conversion_plans", expire
    )
    monkeypatch.setattr(retention, "expire_mask_annotation_revisions", expire)
    monkeypatch.setattr(
        retention, "refresh_raster_mask_active_geometries_safely", metrics
    )
    monkeypatch.setattr(retention, "collect_raster_masks", collect)
    result = await retention.RasterMaskRetentionWorkflow(
        db, session_factory=factory
    ).purge_unreferenced_raster_masks(dry_run=True, scan_pages=3, delete_limit=7)
    assert events == ["expire"] * 4 + [
        "commit_expiry",
        "metrics",
        "release_metrics",
        "rpc",
    ]
    assert result["referenced"] is None and result["errors"] == 1
    assert result["expired_tracker_candidates"] == 2
    assert result["deleted"] == 0 and result["dry_run"] is True
