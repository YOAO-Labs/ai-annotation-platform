"""Labeling keeps GC authority and refuses remote deletion without durable fences."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from aap_assets_mask_content.contract import (
    Candidate,
    CandidatePage,
    DeleteResult,
    MaskContentFailure,
)
from app.services import raster_mask_storage as masks
from app.config import settings

KEY = "raster-masks/sha256/aa/aa/" + "a" * 64 + ".json"
OTHER_KEY = "raster-masks/sha256/bb/bb/" + "b" * 64 + ".json"
NOW = datetime.now(timezone.utc)


class Result:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class Database:
    def __init__(self, *, upload_after_commit=False):
        self.locked = False
        self.events = []
        self.reservation = False
        self.upload_after_commit = upload_after_commit
        self.commits = 0

    async def execute(self, statement, parameters=None):
        sql = str(statement)
        if "pg_advisory_xact_lock" in sql:
            self.locked = True
            self.events.append("lock")
        elif sql.startswith("DELETE"):
            assert self.locked
            self.events.append("retire_reservations")
            self.reservation = False
        elif "raster_mask_uploads.id" in sql:
            assert self.locked
            self.events.append("reservation_check")
            return Result(1 if self.reservation else None)
        return Result()

    async def commit(self):
        self.locked = False
        self.commits += 1
        self.events.append("commit")
        if self.upload_after_commit and self.commits == 1:
            self.reservation = True

    async def rollback(self):
        self.locked = False
        self.events.append("rollback")


def _candidate(key=KEY):
    return Candidate(
        key=key,
        etag='"observed-etag"',
        byte_size=56,
        last_modified=NOW - timedelta(days=2),
    )


def _owner(monkeypatch, provider):
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "")

    @contextmanager
    def selected(_storage, *, cleanup=False):
        assert cleanup
        yield provider

    monkeypatch.setattr(masks, "mask_content_owner", selected)


async def test_gc_refuses_remote_mutation_before_any_storage_or_sql(monkeypatch):
    monkeypatch.setattr(settings, "assets_mask_cleanup_url", "http://cleanup.test")

    def forbidden(*args, **kwargs):
        pytest.fail("unfenced remote GC accessed storage")

    monkeypatch.setattr(masks, "mask_content_owner", forbidden)
    db = Database()
    with pytest.raises(MaskContentFailure) as captured:
        await masks.purge_unreferenced_raster_mask_objects(
            db,
            set(),
            NOW,
            is_referenced=forbidden,
        )
    assert captured.value.code == "storage_unavailable"
    assert db.events == []


@pytest.mark.parametrize("race", ["none", "new_root", "new_upload", "recent_upload"])
async def test_gc_rechecks_current_roots_and_uploads_before_locked_delete(
    monkeypatch, race
):
    db = Database(upload_after_commit=race == "new_upload")
    db.reservation = race == "recent_upload"
    root_checks = 0
    deleted = []

    async def referenced(_db, key):
        nonlocal root_checks
        assert _db is db and db.locked and key == KEY
        root_checks += 1
        db.events.append("root_check")
        return race == "new_root" and root_checks == 2

    class Owner:
        def list(self, request):
            assert request.cursor is None
            return CandidatePage(items=(_candidate(),), cursor=None)

        def delete(self, request):
            assert db.locked and root_checks == 2
            assert request.etag == '"observed-etag"'
            assert request.last_modified == _candidate().last_modified
            db.events.append("owner_delete")
            deleted.append(request.object_key)
            return DeleteResult(object_key=request.object_key, deleted=True)

    _owner(monkeypatch, Owner())
    result = await masks.purge_unreferenced_raster_mask_objects(
        db, set(), NOW - timedelta(hours=24), is_referenced=referenced
    )
    assert result == {
        "scanned": 1,
        "eligible": 1,
        "deleted": int(race == "none"),
        "errors": 0,
    }
    assert deleted == ([KEY] if race == "none" else [])
    assert db.events[:3] == ["lock", "root_check", "reservation_check"]
    if race == "recent_upload":
        assert "retire_reservations" not in db.events
    else:
        assert db.events[3:5] == ["retire_reservations", "commit"]
    assert not db.locked


async def test_gc_pages_past_retained_objects_and_counts_conditional_refusal(
    monkeypatch,
):
    db = Database()
    cursors = []

    async def referenced(_db, _key):
        return False

    class Owner:
        def list(self, request):
            cursors.append(request.cursor)
            if request.cursor is None:
                return CandidatePage(items=(_candidate(),), cursor="next")
            return CandidatePage(items=(_candidate(OTHER_KEY),), cursor=None)

        def delete(self, _request):
            assert db.locked
            raise MaskContentFailure("content_changed")

    _owner(monkeypatch, Owner())
    result = await masks.purge_unreferenced_raster_mask_objects(
        db, {KEY}, NOW - timedelta(hours=24), is_referenced=referenced
    )
    assert cursors == [None, "next"]
    assert result == {"scanned": 2, "eligible": 1, "deleted": 0, "errors": 1}
    assert db.events[-1] == "rollback"


async def test_gc_rejects_a_nonprogressing_owner_cursor(monkeypatch):
    db = Database()

    class Owner:
        def list(self, _request):
            return CandidatePage(items=(), cursor="same")

    _owner(monkeypatch, Owner())
    with pytest.raises(MaskContentFailure) as captured:
        await masks.purge_unreferenced_raster_mask_objects(
            db, set(), NOW, is_referenced=None, dry_run=True
        )
    assert captured.value.code == "invalid_response"
    assert db.events == ["rollback"]
