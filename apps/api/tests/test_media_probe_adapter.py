"""Consumer checks for the independent probe's typed and complete wire results."""

import hashlib
import json
import subprocess

import httpx
import pytest

from app.services import media_probe


_METADATA = {
    "duration_ms": 2002,
    "fps": 29.97,
    "frame_count": 60,
    "width": 1280,
    "height": 720,
    "codec": "h264",
}
_FRAMES = [
    {
        "frame_index": i,
        "pts_ms": i * 33,
        "is_keyframe": i == 0,
        "pict_type": "I" if i == 0 else "P",
        "byte_offset": 48 + i * 100,
    }
    for i in range(2)
]
_PACKETS = [
    {
        "frame_index": i,
        "pts_ms": pts,
        "duration_ms": 33,
        "is_keyframe": i == 10,
        "size_bytes": 100,
        "offset_in_chunk": offset,
    }
    for i, pts, offset in ((10, 0, 48), (12, 67, 148), (11, 33, 248))
]


class SplitStream(httpx.SyncByteStream):
    def __init__(self, body: bytes, *, fail: bool = False):
        self.body = body
        self.fail = fail

    def __iter__(self):
        for offset in range(0, len(self.body), 7):
            yield self.body[offset : offset + 7]
        if self.fail:
            raise httpx.ReadError("controlled incomplete stream")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.mp4"
    path.write_bytes(b"owned test media bytes")
    return path


@pytest.fixture
def select_remote(monkeypatch):
    monkeypatch.setattr(media_probe.settings, "media_probe_url", "http://probe.local")
    monkeypatch.setattr(media_probe.settings, "media_probe_token", "t" * 40)
    original_client = httpx.Client

    def install(handler):
        def make_client(**kwargs):
            assert kwargs["trust_env"] is False
            assert kwargs["follow_redirects"] is False
            return original_client(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(media_probe.httpx, "Client", make_client)

    return install


def _ndjson(rows, kind="frame", count=None):
    records = [{"type": kind, "data": row} for row in rows]
    lines = b"".join(json.dumps(row).encode() + b"\n" for row in records)
    footer = {
        "type": "complete",
        "count": len(rows) if count is None else count,
        "sha256": hashlib.sha256(lines).hexdigest(),
    }
    return lines + json.dumps(footer).encode() + b"\n"


def _stream_response(body, *, fail=False):
    return httpx.Response(
        200,
        headers={"Content-Type": "application/x-ndjson"},
        stream=SplitStream(body, fail=fail),
    )


def test_remote_metadata_upload_uses_scoped_auth_and_exact_facts(source, select_remote):
    def handler(request):
        assert request.url.path == "/v1/metadata"
        assert request.headers["Authorization"] == "Bearer " + "t" * 40
        assert request.headers["Content-Type"] == "application/octet-stream"
        assert int(request.headers["Content-Length"]) == source.stat().st_size
        assert request.read() == source.read_bytes()
        assert not request.url.query
        return httpx.Response(200, json=_METADATA)

    select_remote(handler)
    assert media_probe.probe_video_file(source) == _METADATA


def test_remote_frame_stream_is_complete_before_returning(source, select_remote):
    select_remote(lambda request: _stream_response(_ndjson(_FRAMES)))
    assert media_probe.probe_video_frame_timetable(source) == _FRAMES


def test_remote_packets_preserve_decode_order_and_presentation_ranks(
    source, select_remote
):
    def handler(request):
        assert request.url.path == "/v1/packets"
        assert request.url.params["start_frame"] == "10"
        return _stream_response(_ndjson(_PACKETS, "packet"))

    select_remote(handler)
    assert media_probe.probe_chunk_samples(source, 10) == _PACKETS


def test_empty_frame_stream_requires_completion_record(source, select_remote):
    select_remote(lambda request: _stream_response(_ndjson([])))
    assert media_probe.probe_video_frame_timetable(source) == []


@pytest.mark.parametrize(
    "body",
    [
        _ndjson(_FRAMES).rsplit(b'{"type": "complete"', 1)[0],
        _ndjson(_FRAMES, count=3),
        _ndjson(_FRAMES).replace(b'"pts_ms": 33', b'"pts_ms": 34'),
        _ndjson([_FRAMES[1]]),
        _ndjson(_FRAMES) + b'{"type":"complete","count":2}\n',
        _ndjson(_FRAMES).rstrip(b"\n"),
        _ndjson([{**_FRAMES[0], "is_keyframe": "false"}]),
        _ndjson([{**_FRAMES[0], "pict_type": "X" * (65 * 1024)}]),
    ],
    ids=[
        "truncated",
        "count",
        "digest",
        "order",
        "trailing",
        "unterminated",
        "type",
        "oversize",
    ],
)
def test_invalid_frame_stream_never_returns_partial_rows(source, select_remote, body):
    select_remote(lambda request: _stream_response(body))
    with pytest.raises(media_probe.MediaProbeError) as failure:
        media_probe.probe_video_frame_timetable(source)
    assert failure.value.code == "invalid_response"


@pytest.mark.parametrize("packets", [_PACKETS[:2], [_PACKETS[0], _PACKETS[0]]])
def test_invalid_packet_ranks_are_rejected(source, select_remote, packets):
    select_remote(lambda request: _stream_response(_ndjson(packets, "packet")))
    with pytest.raises(
        media_probe.MediaProbeError, match="Invalid media-probe response"
    ):
        media_probe.probe_chunk_samples(source, 10)


def test_stream_disconnect_never_returns_partial_rows(source, select_remote):
    select_remote(lambda request: _stream_response(_ndjson(_FRAMES), fail=True))
    with pytest.raises(media_probe.MediaProbeError) as failure:
        media_probe.probe_video_frame_timetable(source)
    assert failure.value.code == "unavailable"


def test_remote_timeout_preserves_existing_worker_failure_class(source, select_remote):
    select_remote(
        lambda request: httpx.Response(
            504, json={"code": "probe_timeout", "message": "ffprobe timed out"}
        )
    )
    with pytest.raises(subprocess.TimeoutExpired) as failure:
        media_probe.probe_video_file(source)
    assert failure.value.timeout == media_probe.local_probe.FFPROBE_TIMEOUT_SECONDS


def test_selected_remote_failure_does_not_run_local_probe(
    source, select_remote, monkeypatch
):
    def disconnected(request):
        raise httpx.ConnectError("controlled peer unavailable", request=request)

    def forbidden_local(*args, **kwargs):
        pytest.fail("selected remote owner must not silently fall back")

    monkeypatch.setattr(media_probe.local_probe, "probe_video_file", forbidden_local)
    select_remote(disconnected)
    with pytest.raises(media_probe.MediaProbeError) as failure:
        media_probe.probe_video_file(source)
    assert failure.value.code == "unavailable"


def test_unconfigured_peer_keeps_local_probe(source, monkeypatch):
    monkeypatch.setattr(media_probe.settings, "media_probe_url", "")
    seen = []

    def local(path):
        seen.append(path)
        return _METADATA

    monkeypatch.setattr(media_probe.local_probe, "probe_video_file", local)
    assert media_probe.probe_video_file(source) == _METADATA
    assert seen == [source]


def test_configured_client_factory_matches_existing_settings_selection(monkeypatch):
    monkeypatch.setattr(media_probe.settings, "media_probe_url", "")
    monkeypatch.setattr(media_probe.settings, "media_probe_token", "unused")
    assert media_probe.configured_probe_client() is None

    monkeypatch.setattr(media_probe.settings, "media_probe_url", "http://probe.local")
    monkeypatch.setattr(media_probe.settings, "media_probe_token", "t" * 40)
    monkeypatch.setattr(media_probe.settings, "media_probe_timeout_seconds", 12.5)
    client = media_probe.configured_probe_client()
    assert isinstance(client, media_probe.MediaProbeClient)
    assert client._base_url == "http://probe.local"
    assert client._timeout_seconds == 12.5
    assert "t" * 40 not in repr(client)


@pytest.mark.parametrize(
    "url,token",
    [
        ("http://user:password@probe.local", "t" * 40),
        ("http://probe.local?token=secret", "t" * 40),
        ("file:///tmp/source", "t" * 40),
        ("http://probe.local", ""),
    ],
)
def test_bad_peer_configuration_fails_without_exposing_values(
    source, monkeypatch, url, token
):
    monkeypatch.setattr(media_probe.settings, "media_probe_url", url)
    monkeypatch.setattr(media_probe.settings, "media_probe_token", token)
    with pytest.raises(media_probe.MediaProbeError) as failure:
        media_probe.probe_video_file(source)
    assert failure.value.code == "invalid_configuration"
    assert "password" not in str(failure.value)
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize(
    "stream_state", ["complete", "missing_footer", "digest_mismatch"]
)
async def test_remote_timetable_only_replaces_committed_rows_after_integrity_check(
    test_engine, select_remote, stream_state
):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.application.video_timetable_rebuild import VideoTimetableRebuildWorkflow
    from app.db.models.dataset import DatasetItem, VideoFrameIndex
    from tests.test_video_frame_service import (
        _cleanup_video_item,
        _make_committed_video_item,
    )

    maker = async_sessionmaker(test_engine, expire_on_commit=False)
    item_id, dataset_id, user_id = await _make_committed_video_item(maker)
    try:
        async with maker() as setup:
            item = await setup.get(DatasetItem, item_id)
            baseline_metadata = item.metadata_
            setup.add(
                VideoFrameIndex(
                    dataset_item_id=item_id,
                    frame_index=0,
                    pts_ms=999,
                    is_keyframe=False,
                )
            )
            await setup.commit()

        class SourceBytes:
            client = None

            def __init__(self):
                self.client = self

            def bucket_for_cache_key(self, key):
                return "owned-test-bucket"

            def download_fileobj(self, *, Bucket, Key, Fileobj):
                Fileobj.write(b"owned test media bytes")

        body = _ndjson(_FRAMES)
        if stream_state == "missing_footer":
            body = body.rsplit(b'{"type": "complete"', 1)[0]
        elif stream_state == "digest_mismatch":
            body = body.replace(b'"pts_ms": 33', b'"pts_ms": 34')
        select_remote(lambda request: _stream_response(body))
        async with maker() as mutation:
            item = await mutation.get(DatasetItem, item_id)
            workflow = VideoTimetableRebuildWorkflow(mutation, SourceBytes())
            if stream_state == "complete":
                assert await workflow.rebuild_item(item) == 2
            else:
                with pytest.raises(media_probe.MediaProbeError):
                    await workflow.rebuild_item(item)
            # Commit even after a rejected result: no partial replacement may exist.
            await mutation.commit()

        async with maker() as observer:
            rows = (
                await observer.execute(
                    select(VideoFrameIndex.frame_index, VideoFrameIndex.pts_ms)
                    .where(VideoFrameIndex.dataset_item_id == item_id)
                    .order_by(VideoFrameIndex.frame_index)
                )
            ).all()
            item = await observer.get(DatasetItem, item_id)
            if stream_state == "complete":
                assert rows == [(0, 0), (1, 33)]
                assert item.metadata_["video"]["frame_timetable_frame_count"] == 2
            else:
                assert rows == [(0, 999)]
                assert item.metadata_ == baseline_metadata
    finally:
        await _cleanup_video_item(maker, item_id, dataset_id, user_id)
