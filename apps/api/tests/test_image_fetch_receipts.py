"""Execute generated fetchers against controlled HTTP and real filesystem paths."""

from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import subprocess
import sys
import threading

import pytest

from app.services.exporting.image_fetch import FETCH_IMAGES_TEMPLATE


@pytest.fixture
def world(tmp_path):
    state = {"body": b"verified image", "length": None, "seen": [], "short": False}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["seen"].append(self.path)
            self.send_response(200)
            self.send_header(
                "Content-Length",
                str(
                    state["length"]
                    if state["length"] is not None
                    else len(state["body"])
                ),
            )
            self.end_headers()
            self.wfile.write(state["body"][:-1] if state["short"] else state["body"])
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.update(root=tmp_path, origin=f"http://127.0.0.1:{server.server_port}")
    (tmp_path / "fetch_images.py").write_text(FETCH_IMAGES_TEMPLATE)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        shutil.rmtree(tmp_path)


def manifest(world, *, paths=None, embedded=False, expired=False):
    expiry = (
        datetime.now(timezone.utc) + timedelta(days=-1 if expired else 1)
    ).isoformat()
    entry = {
        "rel_path": "tasks/one/a.png",
        "archive_paths": paths or ["images/tasks/one/a.png"],
        "sha256": hashlib.sha256(world["body"]).hexdigest(),
        "size_bytes": len(world["body"]),
        "expires_at": expiry,
        "storage_mode": "embedded" if embedded else "managed",
    }
    if not embedded:
        entry["presigned_url"] = "/minio/object%2Fpart?signature=a%2Bb%2Fz&second=1+2"
    return {"images": [entry], "expires_at": expiry}


def run(world, data, *, base=True):
    root = world["root"]
    (root / "images_manifest.json").write_text(json.dumps(data))
    args = [sys.executable, str(root / "fetch_images.py")]
    if base is not False:
        args.extend(["--base-url", world["origin"] if base is True else base])
    result = subprocess.run(args, capture_output=True, text=True, timeout=10)
    assert "signature=" not in result.stdout + result.stderr
    assert not list(root.rglob(".aap-download-*"))
    return result


def test_relative_signed_query_and_all_yolo_destinations_are_verified(world):
    paths = [
        "images/tasks/one/a.png",
        "yolo-det/images/tasks/one/a.png",
        "yolo-seg/images/tasks/one/a.png",
    ]
    data = manifest(world, paths=paths)
    result = run(world, data)
    assert result.returncode == 0, result.stdout + result.stderr
    assert world["seen"] == [data["images"][0]["presigned_url"]] * 3
    assert all((world["root"] / path).read_bytes() == world["body"] for path in paths)
    world["seen"].clear()
    assert run(world, data).returncode == 0
    assert not world["seen"]
    (world["root"] / paths[1]).write_bytes(b"wrong existing image")
    assert run(world, data).returncode == 0
    assert (world["root"] / paths[1]).read_bytes() == world["body"]
    assert len(world["seen"]) == 1


@pytest.mark.parametrize("failure", ["hash", "length", "truncated"])
def test_download_failure_preserves_existing_wrong_file_and_owned_temporary_only(
    world, failure
):
    data = manifest(world)
    target = world["root"] / data["images"][0]["archive_paths"][0]
    target.parent.mkdir(parents=True)
    target.write_bytes(b"prior wrong image")
    unrelated = target.with_suffix(".png.part")
    unrelated.write_bytes(b"not owned")
    if failure == "hash":
        world["body"] = b"x" * len(world["body"])
    elif failure == "length":
        world["length"] = len(world["body"]) + 1
    else:
        world["short"] = True
    assert run(world, data).returncode == 1
    assert target.read_bytes() == b"prior wrong image"
    assert unrelated.read_bytes() == b"not owned"


@pytest.mark.parametrize(
    "base",
    [
        False,
        "https://x/",
        "https://user:pass@x",
        "https://x/path",
        "//x",
        "https://x?token=private",
        "https://x/#private",
        "https://x\\evil",
        "https://x\n",
    ],
)
def test_missing_or_unsafe_origin_is_actionable_and_never_downloads(world, base):
    result = run(world, manifest(world), base=base)
    assert result.returncode == 1 and "--base-url" in result.stdout
    assert not world["seen"]


def test_absolute_signed_url_keeps_original_origin(world):
    data = manifest(world)
    expected = data["images"][0]["presigned_url"]
    data["images"][0]["presigned_url"] = world["origin"] + expected
    assert run(world, data, base="https://different.invalid").returncode == 0
    assert world["seen"] == [expected]


@pytest.mark.parametrize(
    "bad",
    [
        "../escape.png",
        "/absolute.png",
        "a\\b.png",
        "a\x00b",
        "images/../escape.png",
        "images//a.png",
        "images/./a.png",
        "images/new\nline.png",
    ],
)
def test_unsafe_archive_destinations_rejected_before_network(world, bad):
    assert run(world, manifest(world, paths=[bad])).returncode == 1
    assert not world["seen"]


def test_duplicate_destination_and_symlink_rejected_before_network(world):
    assert (
        run(world, manifest(world, paths=["images/a.png", "images/a.png"])).returncode
        == 1
    )
    assert not world["seen"]
    (world["root"] / "other").mkdir()
    (world["root"] / "images").symlink_to(
        world["root"] / "other", target_is_directory=True
    )
    assert run(world, manifest(world, paths=["images/a.png"])).returncode == 1
    assert not world["seen"]


def test_embedded_bytes_remain_verifiable_after_expiry_and_detect_tamper(world):
    data = manifest(
        world,
        embedded=True,
        expired=True,
        paths=["images/a.png", "yolo-det/images/a.png"],
    )
    for path in data["images"][0]["archive_paths"]:
        destination = world["root"] / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(world["body"])
    result = run(world, data, base=False)
    assert result.returncode == 0 and "[ok] embedded SHA-256" in result.stdout
    assert not world["seen"]

    (world["root"] / "yolo-det/images/a.png").write_bytes(b"tampered")
    assert run(world, data, base=False).returncode == 1
    assert not world["seen"]


def test_mixed_embedded_verifies_even_when_relative_managed_link_expired(world):
    data = manifest(world, embedded=True, expired=True, paths=["images/embedded.png"])
    managed = manifest(world, expired=True)["images"][0]
    data["images"].append(managed)
    path = world["root"] / "images/embedded.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(world["body"])
    result = run(world, data, base=False)
    assert result.returncode == 2 and "[ok] embedded SHA-256" in result.stdout
    assert not world["seen"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("sha256", None),
        ("sha256", "not-a-digest"),
        ("size_bytes", True),
        ("size_bytes", -1),
        ("presigned_url", "//host/private"),
        ("presigned_url", "https://user:secret@host/a"),
    ],
)
def test_missing_invalid_receipts_and_unsafe_urls_rejected(world, field, value):
    data = manifest(world)
    data["images"][0][field] = value
    assert run(world, data).returncode == 1
    assert not world["seen"]
