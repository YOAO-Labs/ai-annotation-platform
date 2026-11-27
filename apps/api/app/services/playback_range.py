"""HTTP range selection for verified original content."""

import re
from dataclasses import dataclass

from fastapi import HTTPException

MAX_RANGE_HEADER = 512


_RANGE = re.compile(r"([0-9]*)-([0-9]*)", re.ASCII)


@dataclass(frozen=True)
class PlaybackRange:
    status_code: int
    start: int
    length: int
    content_range: str | None = None


def _invalid_range():
    raise HTTPException(400, detail={"code": "playback_range_invalid"})


def select_range(
    size: int,
    etag: str,
    *,
    request_method: str = "GET",
    range_header: str | None = None,
    if_range: str | None = None,
) -> PlaybackRange:
    """Select a range after authorization, without opening or reading the file."""
    if type(size) is not int or not 0 <= size <= 2**63 - 1:
        raise ValueError("Invalid verified stage size")
    method = request_method.upper()
    if method not in {"GET", "HEAD"}:
        raise ValueError("Playback supports only GET and HEAD")
    full = PlaybackRange(200, 0, size)
    if method == "HEAD" or range_header is None:
        return full
    if if_range is not None and if_range != etag:
        return full
    if (
        not isinstance(range_header, str)
        or len(range_header) > MAX_RANGE_HEADER
        or any(
            (ord(char) < 32 and char != "\t") or ord(char) == 127
            for char in range_header
        )
    ):
        _invalid_range()
    unit, separator, raw = range_header.strip().partition("=")
    if unit.strip().lower() != "bytes":
        return full
    if not separator:
        _invalid_range()
    parts = []
    for value in raw.split(","):
        match = _RANGE.fullmatch(value.strip())
        if match is None or not any(match.groups()):
            _invalid_range()
        left, right = match.groups()
        start = int(left) if left else None
        end = int(right) if right else None
        if start is not None and end is not None and end < start:
            _invalid_range()
        parts.append((start, end))
    if len(parts) > 1:
        return full

    start, end = parts[0]
    if start is None:
        if size == 0 and end > 0:
            return full
        start = max(0, size - end)
        end = size - 1
    else:
        end = size - 1 if end is None else min(end, size - 1)
    if start >= size or end < start:
        raise HTTPException(
            416,
            detail={"code": "playback_range_unsatisfiable"},
            headers={"Content-Range": f"bytes */{size}"},
        )
    return PlaybackRange(206, start, end - start + 1, f"bytes {start}-{end}/{size}")
