"""Complete, scope-checked accumulation of bounded Assets spatial pages."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


_Row = TypeVar("_Row")
_Cursor = TypeVar("_Cursor")


class SpatialPageError(ValueError):
    """A page escaped its request scope or the result is incomplete."""


@dataclass(frozen=True, slots=True)
class SpatialPage(Generic[_Row, _Cursor]):
    """Runtime view of one strictly decoded native page.

    The wire contract remains in ``spatial_contract``. Its decoder maps the
    versioned DTO into this small accumulator view.
    """

    schema: str
    action: str
    scope: object
    snapshot: str
    cursor: _Cursor | None
    rows: tuple[_Row, ...]
    next_cursor: _Cursor | None
    complete: bool
    total: int


async def collect_spatial_pages(
    fetch_page: Callable[[_Cursor | None], Awaitable[object]],
    *,
    decode_page: Callable[[object], SpatialPage[_Row, _Cursor]],
    expected_schema: str,
    expected_action: str,
    expected_scope: object,
    row_identity: Callable[[_Row], Hashable],
    cursor_for_row: Callable[[_Row], _Cursor],
    cursor_order_key: Callable[[_Cursor], Any],
) -> list[_Row]:
    """Fetch every page and expose rows only after the owner snapshot verifies.

    There is intentionally no aggregate page/row limit. Each page is bounded
    by the native wire contract. The opaque ``snapshot`` digest and row total
    are fixed across pages; decoded results are returned only after the final
    page confirms that the total is complete.
    """
    rows: list[_Row] = []
    cursor: _Cursor | None = None
    prior_order: Any = None
    snapshot: str | None = None
    total: int | None = None
    seen_ids: set[Hashable] = set()

    while True:
        raw_page = await fetch_page(cursor)
        try:
            page = decode_page(raw_page)
        except SpatialPageError:
            raise
        except Exception as exc:
            raise SpatialPageError("spatial page violates its wire contract") from exc

        if page.schema != expected_schema or page.action != expected_action:
            raise SpatialPageError("spatial page schema or action changed")
        if page.scope != expected_scope:
            raise SpatialPageError("spatial page escaped its request scope")
        if page.cursor != cursor:
            raise SpatialPageError("spatial page echoed a different cursor")
        if not isinstance(page.snapshot, str) or not page.snapshot:
            raise SpatialPageError("spatial page has no snapshot identity")
        if snapshot is None:
            snapshot = page.snapshot
        elif page.snapshot != snapshot:
            raise SpatialPageError("spatial page snapshot changed")

        if type(page.total) is not int or page.total < 0:
            raise SpatialPageError("spatial page total is invalid")
        if total is None:
            total = page.total
        elif page.total != total:
            raise SpatialPageError("spatial page total changed")
        if type(page.complete) is not bool:
            raise SpatialPageError("spatial page completion flag is invalid")
        if page.complete != (page.next_cursor is None):
            raise SpatialPageError("spatial page cursor and completion disagree")
        if not page.rows and not page.complete:
            raise SpatialPageError("empty spatial page did not advance")

        page_cursors: list[_Cursor] = []
        for row in page.rows:
            identity = row_identity(row)
            if identity in seen_ids:
                raise SpatialPageError("spatial page repeated a row identity")
            seen_ids.add(identity)
            row_cursor = cursor_for_row(row)
            order = cursor_order_key(row_cursor)
            if prior_order is not None:
                try:
                    advances = order > prior_order
                except TypeError as exc:
                    raise SpatialPageError(
                        "spatial page cursors are not comparable"
                    ) from exc
                if not advances:
                    raise SpatialPageError("spatial page cursor order did not advance")
            prior_order = order
            page_cursors.append(row_cursor)
            rows.append(row)
            if total is not None and len(rows) > total:
                raise SpatialPageError("spatial pages exceed their declared total")

        if page.complete:
            if len(rows) != total:
                raise SpatialPageError("spatial page result ended before its total")
            return rows

        assert page.next_cursor is not None  # established by completion agreement
        if not page_cursors or page.next_cursor != page_cursors[-1]:
            raise SpatialPageError("spatial next cursor does not match its last row")
        next_order = cursor_order_key(page.next_cursor)
        if cursor is not None:
            try:
                advances = next_order > cursor_order_key(cursor)
            except TypeError as exc:
                raise SpatialPageError("spatial next cursor is not comparable") from exc
            if not advances:
                raise SpatialPageError("spatial next cursor did not advance")
        cursor = page.next_cursor
