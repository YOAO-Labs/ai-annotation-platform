from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from aap_assets_media.contract import ReadSourceRequest
from app.api.v1.dashboard import annotator as annotator_dashboard
from app.api.v1.dashboard import reviewer as reviewer_dashboard
from app.db.enums import UserRole


@pytest.mark.asyncio
@pytest.mark.parametrize("poster_url", ["https://browser/poster", None])
async def test_annotator_video_batch_cover_never_signs_legacy_poster(
    monkeypatch, poster_url
):
    actor_id, project_id, batch_id, task_id, item_id = (uuid4() for _ in range(5))
    batch = SimpleNamespace(
        id=batch_id,
        project_id=project_id,
        status="active",
        annotator_id=actor_id,
        reviewer_id=None,
        display_id="B-VIDEO",
        name="Video batch",
        total_tasks=1,
        completed_tasks=0,
        review_tasks=0,
        approved_tasks=0,
        rejected_tasks=0,
        review_feedback=None,
        reviewed_at=None,
    )
    responses = [
        [(batch, "Project", project_id)],
        [(batch_id, 0)],
        [
            (
                batch_id,
                task_id,
                item_id,
                "video",
                "video",
                "media-assets/stale.webp",
                "stale",
            )
        ],
    ]

    class FakeDb:
        async def execute(self, _statement):
            return SimpleNamespace(all=lambda: responses.pop(0))

    async def posters(_db, requests):
        assert requests == [
            ReadSourceRequest(
                actor_id=actor_id,
                dataset_item_id=item_id,
                task_id=task_id,
            )
        ]
        return [poster_url]

    signed = []
    monkeypatch.setattr(
        annotator_dashboard,
        "video_poster_urls",
        posters,
    )
    monkeypatch.setattr(
        annotator_dashboard.storage_service,
        "generate_download_url",
        lambda *args, **kwargs: signed.append(args) or "legacy-poster",
    )

    result = await annotator_dashboard.my_batches(
        db=FakeDb(),
        current_user=SimpleNamespace(id=actor_id, role=UserRole.SUPER_ADMIN),
    )

    assert result[0].thumbnail_url == poster_url
    assert result[0].cover_blurhash is None
    assert signed == []


@pytest.mark.asyncio
@pytest.mark.parametrize("poster_url", ["https://browser/poster", None])
async def test_reviewer_video_batch_cover_never_signs_legacy_poster(
    monkeypatch, poster_url
):
    actor_id, project_id, batch_id, task_id, item_id = (uuid4() for _ in range(5))
    batch = SimpleNamespace(
        id=batch_id,
        project_id=project_id,
        annotator_id=None,
        display_id="B-VIDEO",
        name="Video batch",
        total_tasks=1,
        review_tasks=1,
        completed_tasks=0,
        status="reviewing",
        updated_at=None,
    )

    class FakeResult:
        def __init__(self, *, scalar=None, rows=(), one=None, first=None):
            self._scalar = scalar
            self._rows = rows
            self._one = one
            self._first = first

        def scalar(self):
            return self._scalar

        def all(self):
            return list(self._rows)

        def one(self):
            return self._one

        def first(self):
            return self._first

    results = [
        FakeResult(scalar=0),
        FakeResult(scalar=0),
        FakeResult(scalar=0),
        FakeResult(one=SimpleNamespace(approve_n=0, reject_n=0)),
        FakeResult(rows=[]),
        FakeResult(rows=[(batch, "Project")]),
        FakeResult(
            rows=[
                (
                    batch_id,
                    task_id,
                    item_id,
                    "video",
                    "video",
                    "media-assets/stale.webp",
                    "stale",
                )
            ]
        ),
        FakeResult(first=None),
        FakeResult(first=SimpleNamespace(approved_n=0, reopened_n=0)),
        *(FakeResult(scalar=0) for _ in range(9)),
    ]

    class FakeDb:
        async def execute(self, _statement):
            return results.pop(0)

        async def scalar(self, _statement):
            return 0

    async def posters(_db, requests):
        assert requests == [
            ReadSourceRequest(
                actor_id=actor_id,
                dataset_item_id=item_id,
                task_id=task_id,
            )
        ]
        return [poster_url]

    signed = []
    monkeypatch.setattr(reviewer_dashboard, "video_poster_urls", posters)
    monkeypatch.setattr(
        reviewer_dashboard.storage_service,
        "generate_download_url",
        lambda *args, **kwargs: signed.append(args) or "legacy-poster",
    )

    result = await reviewer_dashboard.reviewer_dashboard(
        db=FakeDb(),
        current_user=SimpleNamespace(id=actor_id, role=UserRole.SUPER_ADMIN),
    )

    assert result.reviewing_batches[0].thumbnail_url == poster_url
    assert result.reviewing_batches[0].cover_blurhash is None
    assert signed == []
