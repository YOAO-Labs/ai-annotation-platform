"""Real Go process acceptance; no Python runtime or business credentials in peer."""

import asyncio
import json
import os
import selectors
import subprocess
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from experiments.audit_summary import AuditSummaryUnavailable, monthly_audit_summary

_ROOT = Path(__file__).resolve().parents[4]
_MODULE = _ROOT / "apps/audit-summary"


@pytest.fixture(scope="module")
def summary_binary(tmp_path_factory):
    root = tmp_path_factory.mktemp("audit-summary-peer")
    binary = root / "audit-summary"
    env = {
        key: os.environ[key] for key in ("PATH", "HOME", "GOCACHE") if key in os.environ
    }
    env["GOPROXY"] = "off"
    subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=_MODULE,
        env=env,
        check=True,
        capture_output=True,
        timeout=60,
    )
    return binary


@contextmanager
def _peer(binary, address="127.0.0.1:0"):
    process = subprocess.Popen(
        [str(binary), "-listen", address],
        cwd=binary.parent,
        env={},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=15), "Go peer did not report readiness"
        ready = json.loads(process.stdout.readline())
        assert ready["event"] == "ready" and ready["service"] == "audit-summary"
        yield process, ready["address"]
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


async def test_go_peer_lost_response_inflight_shutdown_restart_and_exact_replay(
    summary_binary,
):
    request = json.loads((_MODULE / "testdata/mixed.request.json").read_text())
    expected = json.loads((_MODULE / "testdata/mixed.response.json").read_text())

    def public_counts(value):
        if isinstance(value, dict):
            return {
                key: int(item)
                if key in {"event_count", "error_count"}
                else public_counts(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [public_counts(item) for item in value]
        return value

    async def query():
        return await monthly_audit_summary(
            base_url=f"http://{address}",
            month=request["month"],
            materialized_through=date.fromisoformat(request["materialized_through"])
            if request["materialized_through"]
            else None,
            rows=[
                SimpleNamespace(
                    **{
                        **row,
                        "day": date.fromisoformat(row["day"]),
                        "event_count": int(row["event_count"]),
                    }
                )
                for row in request["rows"]
            ],
        )

    with _peer(summary_binary) as (process, address):
        async with httpx.AsyncClient(trust_env=False) as client:
            async with client.stream(
                "POST", f"http://{address}/v1/audit/monthly-summary", json=request
            ) as abandoned:
                assert abandoned.status_code == 200
                # The peer computed a reply; the caller deliberately loses its body.
        result = await query()
        assert result.model_dump(mode="json") == public_counts(expected)
        host, port = address.rsplit(":", 1)
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(
            b"POST /v1/audit/monthly-summary HTTP/1.1\r\nHost: localhost\r\n"
            b"Content-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
            b'{"month":"2026-10",'
        )
        await writer.drain()
        process.terminate()
        assert await asyncio.to_thread(process.wait, timeout=10) == 0
        await asyncio.wait_for(reader.read(), timeout=5)
        writer.close()
        await writer.wait_closed()
    with pytest.raises(AuditSummaryUnavailable, match="unavailable"):
        await query()
    with _peer(summary_binary, address):
        assert (await query()).model_dump(mode="json") == result.model_dump(mode="json")
