from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from aap_assets_catalog.spatial_pages import (
    SpatialPage,
    SpatialPageError,
    collect_spatial_pages,
)


_SCHEMA = "aap-assets-spatial/test"
_ACTION = "read_spatial_layout"
_SCOPE = {"dataset_id": "dataset-1"}


def _page(
    rows: list[dict[str, Any]],
    *,
    cursor: str | None = None,
    next_cursor: str | None,
    complete: bool,
    total: int,
    snapshot: str = "snapshot-1",
    scope: object = _SCOPE,
    action: str = _ACTION,
    schema: str = _SCHEMA,
) -> SpatialPage[dict[str, Any], str]:
    return SpatialPage(
        schema=schema,
        action=action,
        scope=scope,
        snapshot=snapshot,
        cursor=cursor,
        rows=tuple(rows),
        next_cursor=next_cursor,
        complete=complete,
        total=total,
    )


def _collector(pages: dict[str | None, SpatialPage]):
    requested: list[str | None] = []

    async def fetch(cursor: str | None):
        requested.append(cursor)
        return pages[cursor]

    async def run():
        rows = await collect_spatial_pages(
            fetch,
            decode_page=lambda value: value,
            expected_schema=_SCHEMA,
            expected_action=_ACTION,
            expected_scope=_SCOPE,
            row_identity=lambda row: row["id"],
            cursor_for_row=lambda row: row["id"],
            cursor_order_key=lambda cursor: cursor,
        )
        return rows, requested

    return run


@pytest.mark.asyncio
async def test_collect_spatial_pages_validates_snapshot_and_full_total():
    rows = [{"id": "a", "frame_index": 0}, {"id": "b", "frame_index": 1}]
    run = _collector(
        {
            None: _page(
                rows[:1],
                next_cursor="a",
                complete=False,
                total=2,
            ),
            "a": _page(
                rows[1:],
                cursor="a",
                next_cursor=None,
                complete=True,
                total=2,
            ),
        }
    )
    collected, requested = await run()
    assert collected == rows
    assert requested == [None, "a"]


@pytest.mark.asyncio
async def test_collect_spatial_pages_accepts_empty_complete_result():
    run = _collector(
        {
            None: _page(
                [],
                next_cursor=None,
                complete=True,
                total=0,
            )
        }
    )
    collected, requested = await run()
    assert collected == []
    assert requested == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate_first, mutate_second, message",
    [
        (None, lambda page: replace(page, schema="unknown"), "schema or action"),
        (None, lambda page: replace(page, action="other"), "schema or action"),
        (
            None,
            lambda page: replace(page, scope={"dataset_id": "dataset-2"}),
            "scope",
        ),
        (None, lambda page: replace(page, snapshot="snapshot-2"), "snapshot changed"),
        (None, lambda page: replace(page, total=3), "total changed"),
        (
            None,
            lambda page: replace(page, complete=False, next_cursor="z"),
            "last row",
        ),
        (lambda page: replace(page, complete=True), None, "cursor and completion"),
        (None, lambda page: replace(page, cursor="wrong"), "echoed a different cursor"),
    ],
)
async def test_collect_spatial_pages_rejects_scope_snapshot_and_cursor_drift(
    mutate_first, mutate_second, message
):
    rows = [{"id": "a"}, {"id": "b"}]
    first = _page(
        rows[:1],
        next_cursor="a",
        complete=False,
        total=2,
    )
    # The second-page mutation must be observed before any accumulated rows escape.
    second = _page(
        rows[1:],
        cursor="a",
        next_cursor=None,
        complete=True,
        total=2,
    )
    pages = {
        None: mutate_first(first) if mutate_first is not None else first,
        "a": mutate_second(second) if mutate_second is not None else second,
    }
    with pytest.raises(SpatialPageError, match=message):
        await _collector(pages)()


@pytest.mark.asyncio
async def test_collect_spatial_pages_rejects_duplicate_ids_and_incomplete_total():
    duplicate_rows = [{"id": "a"}, {"id": "a"}]
    duplicate_pages = {
        None: _page(
            duplicate_rows,
            cursor=None,
            next_cursor="a",
            complete=False,
            total=2,
        ),
        "a": _page(
            [{"id": "b"}],
            cursor="a",
            next_cursor=None,
            complete=True,
            total=3,
        ),
    }
    with pytest.raises(SpatialPageError, match="repeated a row identity"):
        await _collector(duplicate_pages)()

    row = {"id": "a"}
    short_result = {
        None: _page(
            [row],
            cursor=None,
            next_cursor=None,
            complete=True,
            total=2,
        )
    }
    with pytest.raises(SpatialPageError, match="ended before its total"):
        await _collector(short_result)()


@pytest.mark.asyncio
async def test_collect_spatial_pages_has_no_aggregate_page_limit():
    rows = [{"id": f"{index:05d}"} for index in range(1100)]
    pages = {
        None: _page(
            rows[:1],
            cursor=None,
            next_cursor=rows[0]["id"],
            complete=False,
            total=len(rows),
        )
    }
    for index in range(1, len(rows)):
        cursor = rows[index - 1]["id"]
        page = rows[index : index + 1]
        pages[cursor] = _page(
            page,
            cursor=cursor,
            next_cursor=page[0]["id"] if index < len(rows) - 1 else None,
            complete=index == len(rows) - 1,
            total=len(rows),
        )
    collected, requested = await _collector(pages)()
    assert len(collected) == 1100
    assert len(requested) == 1100
