from __future__ import annotations

import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import uuid

import pytest

from aap_assets_mask_content import codec
from aap_assets_mask_content.gc_client import MaskGcClient
from aap_assets_mask_content.gc_contract import (
    DeleteOperationRequest,
    DeleteStatusRequest,
    GcScanRequest,
)

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "node-peer-gc-token-with-at-least-32-chars"


def _start_node_peer():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    environment = {**os.environ, "MASK_GC_PEER_TOKEN": TOKEN}
    process = subprocess.Popen(
        [node, str(ROOT / "tests" / "mask-gc-node-peer.mjs")],
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
            raise AssertionError("Node Mask GC peer did not become ready")
        ready = json.loads(process.stdout.readline())
        return process, ready
    except Exception:
        process.terminate()
        process.wait(timeout=5)
        raise


def test_python_gc_client_uses_the_independent_node_wire_contract():
    process, ready = _start_node_peer()
    try:
        origin = f"http://127.0.0.1:{ready['port']}"
        operation_id = uuid.UUID("2d639c8a-0fd8-4dcb-9f90-26cb8a752f3a")
        request = DeleteOperationRequest(
            operation_id=operation_id,
            object_key=codec.rle_object_key("a" * 64),
            etag="opaque-etag-1",
            last_modified="2026-10-19T12:30:00Z",
        )
        with MaskGcClient(origin, TOKEN) as client:
            page = client.scan(GcScanRequest(limit=10))
            execution = client.execute(request)
            missing = client.status(DeleteStatusRequest(operation_id=operation_id))

        assert page.items == ()
        assert page.complete
        assert execution.request == request
        assert execution.state == "indeterminate"
        assert missing.state == "not_found"
        assert missing.request is None
    finally:
        process.terminate()
        process.wait(timeout=5)
