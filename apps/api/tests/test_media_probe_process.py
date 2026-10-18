"""Use the same real API consumer against Python and non-Python probe peers."""

import importlib.util
from pathlib import Path

import pytest

from aap_media_probe import probe
from app.services import media_probe


def test_probe_consumer_switches_language_without_changing_business_results(
    tmp_path, monkeypatch
):
    root = Path(__file__).resolve().parents[3]
    peer_tests = root / "apps/media-probe/tests/replacement/test_node_peer.py"
    spec = importlib.util.spec_from_file_location(
        "media_probe_acceptance_peers", peer_tests
    )
    assert spec is not None and spec.loader is not None
    peers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(peers)
    video = peers._ffmpeg_fixture(tmp_path)
    expected = (
        probe.probe_video_file(video),
        probe.probe_video_frame_timetable(video),
        probe.probe_chunk_samples(video, 37),
    )
    monkeypatch.setattr(media_probe.settings, "media_probe_token", peers.TOKEN)
    monkeypatch.setattr(media_probe.settings, "media_probe_timeout_seconds", 10)
    for name, start in (
        ("python", peers._start_python_service),
        ("node", peers._start_node),
    ):
        process, url = start(tmp_path / name)
        monkeypatch.setattr(media_probe.settings, "media_probe_url", url)
        try:
            assert (
                media_probe.probe_video_file(video),
                media_probe.probe_video_frame_timetable(video),
                media_probe.probe_chunk_samples(video, 37),
            ) == expected
        finally:
            peers._stop(process)
    monkeypatch.setattr(media_probe.settings, "media_probe_timeout_seconds", 1)
    with pytest.raises(media_probe.MediaProbeError) as failure:
        media_probe.probe_video_file(video)
    assert failure.value.code == "unavailable"
