"""Run standalone LiDAR fetch scripts against an actual controlled HTTP source."""

from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from app.services.exporting.lidar_fetch import (
    FETCH_LIDAR_IMAGES_TEMPLATE,
    FETCH_POINTCLOUDS_TEMPLATE,
)


@pytest.fixture
def media_server():
    state = SimpleNamespace(
        body=b"same original media\0\xff" * 200,
        sent=None,
        length="actual",
        status=200,
        requests=[],
    )

    class Media(BaseHTTPRequestHandler):
        def do_GET(self):
            state.requests.append(self.path)
            self.send_response(state.status)
            if state.length is not None:
                length = len(state.body) if state.length == "actual" else state.length
                self.send_header("Content-Length", str(length))
            self.end_headers()
            try:
                self.wfile.write(state.body if state.sent is None else state.sent)
                self.wfile.flush()
                if state.sent is not None:
                    self.connection.shutdown(socket.SHUT_RDWR)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Media)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/media?signature=never-print-this-signature"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.fixture(params=["pointclouds", "images"])
def fetcher(tmp_path, media_server, request):
    kind = request.param
    root, outside = tmp_path / "export", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    template = (
        FETCH_POINTCLOUDS_TEMPLATE
        if kind == "pointclouds"
        else FETCH_LIDAR_IMAGES_TEMPLATE
    )
    script = root / f"fetch_{kind}.py"
    script.write_text(template)
    relative = (
        "scene-a/lidar/frame.pcd"
        if kind == "pointclouds"
        else "front/scene-a/camera/frame.jpg"
    )
    directory = "velodyne" if kind == "pointclouds" else "images"
    state = SimpleNamespace(
        root=root,
        outside=outside,
        script=script,
        template=template,
        kind=kind,
        directory=directory,
        relative=relative,
        server=media_server,
        destination=root / directory / relative,
    )

    def write(*, items=None, expires_at="future", **overrides):
        item = {
            "rel_path": relative,
            "presigned_url": media_server.url,
            "size_bytes": len(media_server.body),
            "sha256": hashlib.sha256(media_server.body).hexdigest(),
            "frame": "scene-a/frame",
            "camera": "front",
            "dataset_id": "legacy-compatible",
            **overrides,
        }
        payload = {kind: [item] if items is None else items}
        if expires_at == "future":
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        if expires_at is not None:
            payload["expires_at"] = expires_at
        (root / f"{kind}_manifest.json").write_text(json.dumps(payload))
        return item

    def run():
        environment = dict(os.environ)
        environment.update(
            {
                "PYTHONDONTWRITEBYTECODE": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "http_proxy": "",
                "https_proxy": "",
                "HTTP_PROXY": "",
                "HTTPS_PROXY": "",
            }
        )
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        assert media_server.url not in result.stdout + result.stderr
        assert "never-print-this-signature" not in result.stdout + result.stderr
        assert list(root.rglob(".aap-download-*")) == []
        return result

    state.write, state.run = write, run
    try:
        yield state
    finally:
        shutil.rmtree(root)
        shutil.rmtree(outside)


@pytest.mark.parametrize("content_length", ["actual", None])
def test_actual_script_streams_exact_bytes_with_or_without_http_length_and_verifies_existing(
    fetcher, content_length
):
    world = fetcher
    world.server.body = bytes(range(256)) * 2300  # More than two streaming chunks.
    world.server.length = content_length
    world.write()
    result = world.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert world.destination.read_bytes() == world.server.body
    assert len(world.server.requests) == 1
    result = world.run()
    assert result.returncode == 0
    assert (
        len(world.server.requests) == 1
    )  # Existing bytes were fully verified locally.


def test_existing_same_length_wrong_content_is_replaced_only_after_full_verification(
    fetcher,
):
    world = fetcher
    world.destination.parent.mkdir(parents=True)
    world.destination.write_bytes(b"x" * len(world.server.body))
    foreign = world.destination.with_suffix(".part")
    foreign.write_bytes(b"unrelated partial download")
    world.write(sha256=hashlib.sha256(world.server.body).hexdigest().upper())
    assert world.run().returncode == 0
    assert world.destination.read_bytes() == world.server.body
    assert foreign.read_bytes() == b"unrelated partial download"
    assert len(world.server.requests) == 1


@pytest.mark.parametrize(
    "damage",
    ["hash", "header_length", "eof_short", "overflow", "disconnect", "http_status"],
)
def test_failed_transport_or_receipt_never_publishes_or_overwrites_target(
    fetcher, damage
):
    world = fetcher
    item = world.write()
    if damage == "hash":
        item["sha256"] = "0" * 64
    elif damage == "header_length":
        world.server.length = len(world.server.body) + 1
    elif damage == "eof_short":
        item["size_bytes"] += 1
        world.server.length = None
    elif damage == "overflow":
        item["size_bytes"] -= 1
        world.server.length = None
    elif damage == "disconnect":
        world.server.sent = world.server.body[:10]
    else:
        world.server.status = 404
    world.write(items=[item])
    assert world.run().returncode == 1
    assert not world.destination.exists()
    world.destination.parent.mkdir(parents=True, exist_ok=True)
    world.destination.write_bytes(b"keep existing target")
    assert world.run().returncode == 1
    assert world.destination.read_bytes() == b"keep existing target"


@pytest.mark.parametrize(
    "receipt,value",
    [
        ("sha256", None),
        ("sha256", ""),
        ("sha256", "g" * 64),
        ("sha256", "0" * 64 + "\n"),
        ("sha256", 123),
        ("size_bytes", None),
        ("size_bytes", True),
        ("size_bytes", "10"),
        ("size_bytes", -1),
        ("size_bytes", 1.5),
        ("size_bytes", 256 * 1024 * 1024 + 1),
    ],
)
def test_missing_or_invalid_receipts_are_rejected_before_http_or_target_creation(
    fetcher, receipt, value
):
    world = fetcher
    world.write(**{receipt: value})
    assert world.run().returncode == 1
    assert not world.server.requests and not (world.root / world.directory).exists()


@pytest.mark.parametrize("expires_at", [None, "invalid", "2999-01-01T00:00:00", True])
def test_fixed_manifest_deadline_is_required_and_timezone_aware(fetcher, expires_at):
    world = fetcher
    world.write(expires_at=expires_at)
    assert world.run().returncode == 1
    assert not world.server.requests


def test_expired_manifest_is_refused_even_if_a_local_file_already_matches(fetcher):
    world = fetcher
    world.destination.parent.mkdir(parents=True)
    world.destination.write_bytes(world.server.body)
    world.write(expires_at="2000-01-01T00:00:00+00:00")
    assert world.run().returncode == 2
    assert not world.server.requests
    assert world.destination.read_bytes() == world.server.body


def test_earlier_item_deadline_is_not_extended_by_the_manifest(fetcher):
    world = fetcher
    item = world.write()
    item["expires_at"] = "2000-01-01T00:00:00Z"
    world.write(items=[item])
    assert world.run().returncode == 2 and not world.server.requests


@pytest.mark.parametrize(
    "relative",
    [
        "../escape.pcd",
        "/absolute.pcd",
        r"..\escape.pcd",
        r"C:\escape.pcd",
        "a//b.pcd",
        "a/./b.pcd",
        "a/../b.pcd",
        "a/\0b.pcd",
        "//host/share.pcd",
        "a:b.pcd",
        "",
    ],
)
def test_unsafe_logical_paths_are_refused_before_any_download(fetcher, relative):
    world = fetcher
    world.write(rel_path=relative)
    assert world.run().returncode == 1
    assert not world.server.requests and not list(world.outside.iterdir())


@pytest.mark.parametrize(
    "link_kind", ["root", "parent_outside", "parent_inside", "target", "dangling"]
)
def test_symlinks_are_never_traversed_including_internal_and_dangling_links(
    fetcher, link_kind
):
    world = fetcher
    outside_file = world.outside / "do-not-overwrite"
    outside_file.write_bytes(b"protected")
    output = world.root / world.directory
    if link_kind == "root":
        output.symlink_to(world.outside, target_is_directory=True)
    elif link_kind in {"parent_outside", "parent_inside"}:
        output.mkdir()
        target = world.outside
        if link_kind == "parent_inside":
            target = world.root / "other-owned-directory"
            target.mkdir()
        (output / world.relative.split("/")[0]).symlink_to(
            target, target_is_directory=True
        )
    else:
        world.destination.parent.mkdir(parents=True)
        target = outside_file if link_kind == "target" else world.outside / "missing"
        world.destination.symlink_to(target)
    world.write()
    assert world.run().returncode == 1
    assert not world.server.requests and outside_file.read_bytes() == b"protected"
    assert list(world.outside.iterdir()) == [outside_file]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "/relative",
        "ftp://host/file",
        "http://user:secret@host/file",
        "http://host/\nfile",
    ],
)
def test_only_absolute_http_media_urls_are_accepted(fetcher, url):
    world = fetcher
    world.write(presigned_url=url)
    result = world.run()
    assert result.returncode == 1
    assert (
        not world.server.requests and "user:secret" not in result.stdout + result.stderr
    )


def test_duplicate_destinations_fail_preflight_before_partial_materialization(fetcher):
    world = fetcher
    item = world.write()
    world.write(items=[item, item])
    assert world.run().returncode == 1 and not world.server.requests
    assert not (world.root / world.directory).exists()


def _namespace(world):
    namespace = {
        "__file__": str(world.script),
        "__name__": "test_generated_lidar_fetch",
    }
    exec(compile(world.template, str(world.script), "exec"), namespace)
    return namespace


def test_failed_exclusive_temp_creation_never_removes_someone_elses_file(
    fetcher, monkeypatch
):
    world = fetcher
    world.write()
    world.destination.parent.mkdir(parents=True)
    foreign = world.destination.parent / ".aap-download-collision"
    foreign.write_bytes(b"not this attempt")
    namespace = _namespace(world)
    monkeypatch.setattr(
        namespace["uuid"], "uuid4", lambda: SimpleNamespace(hex="collision")
    )
    assert namespace["main"]() == 1
    assert foreign.read_bytes() == b"not this attempt"
    assert not world.destination.exists() and not world.server.requests


def test_atomic_replace_failure_cleans_only_owned_temp_and_keeps_previous_target(
    fetcher, monkeypatch
):
    world = fetcher
    world.write()
    world.destination.parent.mkdir(parents=True)
    world.destination.write_bytes(b"previous verified export")
    namespace = _namespace(world)

    def failure(*args, **kwargs):
        raise PermissionError("replace denied")

    with monkeypatch.context() as patch:
        patch.setattr(namespace["os"], "replace", failure)
        assert namespace["main"]() == 1
    assert world.destination.read_bytes() == b"previous verified export"
    assert list(world.root.rglob(".aap-download-*")) == []
    assert len(world.server.requests) == 1


def test_file_wrapper_construction_failure_closes_descriptor_and_owned_temp(
    fetcher, monkeypatch
):
    world = fetcher
    world.write()
    namespace = _namespace(world)
    descriptors = []

    def failure(descriptor, mode):
        descriptors.append(descriptor)
        raise RuntimeError("file wrapper failed")

    with monkeypatch.context() as patch:
        patch.setattr(namespace["os"], "fdopen", failure)
        assert namespace["main"]() == 1
    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    assert not world.destination.exists() and not world.server.requests
    assert list(world.root.rglob(".aap-download-*")) == []


def test_portable_branch_keeps_receipts_and_static_symlink_protection(fetcher):
    world = fetcher
    world.write()
    namespace = _namespace(world)
    namespace["_DIRFD"] = False
    assert namespace["main"]() == 0
    assert world.destination.read_bytes() == world.server.body
    world.destination.unlink()
    target = world.outside / "sentinel"
    target.write_bytes(b"safe")
    world.destination.symlink_to(target)
    assert namespace["main"]() == 1
    assert target.read_bytes() == b"safe" and len(world.server.requests) == 1
