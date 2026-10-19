from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import subprocess

import pytest

from aap_assets_mask_content.client import MaskContentClient
from aap_assets_mask_content.contract import (
    DeleteRequest,
    GetRequest,
    ListRequest,
    MaskContentFailure,
    PutRequest,
    RleContent,
)

ROOT = Path(__file__).resolve().parents[1]
CONTENT_TOKEN = "node-peer-content-token-0123456789"
CLEANUP_TOKEN = "node-peer-cleanup-token-0123456789"


def _start_node_peer():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    environment = {
        **os.environ,
        "MASK_CONTENT_PEER_CONTENT_TOKEN": CONTENT_TOKEN,
        "MASK_CONTENT_PEER_CLEANUP_TOKEN": CLEANUP_TOKEN,
    }
    process = subprocess.Popen(
        [node, str(ROOT / "tests" / "mask-content-node-peer.mjs")],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        readable, _writable, _exceptional = select.select([process.stdout], [], [], 5)
        if not readable:
            raise AssertionError("Node Mask peer did not become ready")
        ready = json.loads(process.stdout.readline())
        return process, ready
    except Exception:
        process.terminate()
        process.wait(timeout=5)
        raise


def test_node_peer_uses_the_same_canonical_bytes_and_hash():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    subprocess.run(
        [node, str(ROOT / "tests" / "contract-peer.mjs")],
        check=True,
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_python_client_interoperates_with_independent_node_http_provider():
    process, ready = _start_node_peer()
    try:
        content = RleContent(encoding="coco_rle", size=(2, 2), counts=(1, 3))
        request = PutRequest(content=content)
        content_origin = f"http://127.0.0.1:{ready['content_port']}"
        cleanup_origin = f"http://127.0.0.1:{ready['cleanup_port']}"
        with MaskContentClient(content_origin, CONTENT_TOKEN) as content_client:
            stored = content_client.put(request)
            assert content_client.get(
                GetRequest(reference=stored.reference)
            ).content == (content)

        with MaskContentClient(cleanup_origin, CLEANUP_TOKEN) as cleanup_client:
            with pytest.raises(MaskContentFailure) as wrong_role:
                cleanup_client.get(GetRequest(reference=stored.reference))
            assert wrong_role.value.code == "permission_denied"

            page = cleanup_client.list(ListRequest(limit=10))
            assert len(page.items) == 1
            candidate = page.items[0]
            assert candidate.key == stored.reference.object_key
            deleted = cleanup_client.delete(
                DeleteRequest(
                    object_key=candidate.key,
                    etag=candidate.etag,
                    last_modified=candidate.last_modified,
                )
            )
            assert deleted.deleted is True

        with MaskContentClient(content_origin, CONTENT_TOKEN) as content_client:
            with pytest.raises(MaskContentFailure) as missing:
                content_client.get(GetRequest(reference=stored.reference))
            assert missing.value.code == "object_missing"
    finally:
        process.terminate()
        process.wait(timeout=5)
