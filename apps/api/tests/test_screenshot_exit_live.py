"""Ready screenshot consumer exits preserve another independently owned run.

Run under the module-data verifier, which owns the disposable database and
original store. The consumer uses the actual screenshot catalog transport; it
does not launch a browser or exercise recording or model capabilities.
"""

from __future__ import annotations

import asyncio
import builtins
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import uuid

import pytest
from sqlalchemy import text

from tests.seed_owner_support import fixture_original_reader

pytest_plugins = ("tests.screenshot_seed_support",)
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the owned module-data verifier and screenshot runtime",
    ),
]
ROOT = Path(__file__).resolve().parents[3]
TRANSPORT = ROOT / "apps/web/e2e/helpers/seed-owner-transport.mjs"

# This child is a real consumer of the same private catalog transport used by
# screenshot global setup. READY is emitted only after a successful catalog
# response; it holds no in-flight request while the parent chooses the exit.
CONSUMER = r"""
import { pathToFileURL } from 'node:url';
const { getScreenshotCatalog, closeFixtureTransport } = await import(pathToFileURL(process.argv[2]));
const run = process.argv[3];
const mode = process.argv[4];
let cancel;
const cancelled = new Promise(resolve => { cancel = resolve; });
process.once('SIGINT', () => cancel('SIGINT'));
let failure;
try {
  const url = `http://fixture.local/api/v1/__test/seed/catalog?profile=screenshots&backend_requirements=none&run_id=${run}`;
  const response = await getScreenshotCatalog(url);
  if (!response.ok()) throw new Error(`ready catalog failed: ${response.status()} ${await response.text()}`);
  const catalog = await response.json();
  if (catalog.run_id !== run || catalog.backend_requirements.length !== 0)
    throw new Error('ready catalog identity or backend scope changed');
  console.log(JSON.stringify({event: 'ready', run_id: catalog.run_id}));
  if (mode === 'failure') {
    const input = (async () => { for await (const _ of process.stdin) return 'input'; })();
    const action = await Promise.race([input, cancelled]);
    if (action !== 'input') {
      process.stdin.destroy();
      const error = new Error(`screenshot consumer cancelled by ${action}`);
      error.name = 'AbortError';
      throw error;
    }
    const rejected = await getScreenshotCatalog(url.replace('backend_requirements=none', 'backend_requirements=invalid'));
    if (rejected.ok()) throw new Error('invalid catalog scope unexpectedly succeeded');
    throw new Error(`seed/catalog failed: ${rejected.status()} ${await rejected.text()}`);
  }
  if (mode !== 'cancel') throw new Error('unknown consumer mode');
  const received = await cancelled;
  const error = new Error(`screenshot consumer cancelled by ${received}`);
  error.name = 'AbortError';
  throw error;
} catch (error) {
  failure = error;
}
try {
  await closeFixtureTransport();
} catch (error) {
  failure = failure ? new AggregateError([failure, error], 'Consumer and transport close failed') : error;
}
if (failure) {
  console.error(failure);
  process.exitCode = failure.name === 'AbortError' ? 130 : 1;
}
"""

# All owned business row identities are selected through the immutable run
# manifest, not by display ID, prefix, or content digest. Extra dependent rows
# cover catalog-visible batches, scenes, links and predictions.
ROW_QUERIES = {
    "resources": "SELECT to_jsonb(r) FROM yoao_fixture_v1.fixture_run_resources r WHERE r.run_id=:run ORDER BY r.kind,r.resource_id",
    "users": "SELECT to_jsonb(t) FROM public.users t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='user' WHERE r.run_id=:run ORDER BY t.id",
    "projects": "SELECT to_jsonb(t) FROM public.projects t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='project' WHERE r.run_id=:run ORDER BY t.id",
    "datasets": "SELECT to_jsonb(t) FROM public.datasets t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='dataset' WHERE r.run_id=:run ORDER BY t.id",
    "items": "SELECT to_jsonb(t) FROM public.dataset_items t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='item' WHERE r.run_id=:run ORDER BY t.id",
    "tasks": "SELECT to_jsonb(t) FROM public.tasks t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='task' WHERE r.run_id=:run ORDER BY t.id",
    "annotations": "SELECT to_jsonb(t) FROM public.annotations t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='annotation' WHERE r.run_id=:run ORDER BY t.id",
    "creations": "SELECT to_jsonb(t) FROM public.managed_original_creations t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind IN ('creation','auxiliary') WHERE r.run_id=:run ORDER BY t.id",
    "auxiliary": "SELECT to_jsonb(t) FROM public.dataset_auxiliary_originals t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.id AND r.kind='auxiliary' WHERE r.run_id=:run ORDER BY t.id",
    "scenes": "SELECT to_jsonb(t) FROM public.scenes t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.dataset_id AND r.kind='dataset' WHERE r.run_id=:run ORDER BY t.id",
    "project_datasets": "SELECT to_jsonb(t) FROM public.project_datasets t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.project_id AND r.kind='project' WHERE r.run_id=:run ORDER BY t.id",
    "batches": "SELECT to_jsonb(t) FROM public.task_batches t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.project_id AND r.kind='project' WHERE r.run_id=:run ORDER BY t.id",
    "predictions": "SELECT to_jsonb(t) FROM public.predictions t JOIN yoao_fixture_v1.fixture_run_resources r ON r.resource_id=t.project_id AND r.kind='project' WHERE r.run_id=:run ORDER BY t.id,t.created_at",
}


async def _catalog(seed, run_id):
    response = await seed.catalog(run_id=run_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == str(run_id)
    assert body["backend_requirements"] == []
    return body


async def _rows_and_versions(engine, run_id):
    async with engine.connect() as db:
        digests = {}
        for name, query in ROW_QUERIES.items():
            rows = (await db.execute(text(query), {"run": run_id})).scalars().all()
            encoded = json.dumps(rows, sort_keys=True, separators=(",", ":"))
            digests[name] = (len(rows), hashlib.sha256(encoded.encode()).hexdigest())
        rows = (
            await db.execute(
                text(
                    "SELECT r.kind,c.id,c.location_id,c.bucket,c.object_key,c.version_id,"
                    "c.sha256,c.size_bytes,c.status FROM yoao_fixture_v1.fixture_run_resources r "
                    "JOIN public.managed_original_creations c ON c.id=r.resource_id "
                    "WHERE r.run_id=:run AND r.kind IN ('creation','auxiliary') "
                    "AND r.state='published' ORDER BY c.id"
                ),
                {"run": run_id},
            )
        ).mappings().all()
        count = await db.scalar(
            text("SELECT count(*) FROM yoao_fixture_v1.fixture_run_resources "
                 "WHERE run_id=:run AND kind IN ('creation','auxiliary') AND state='published'"),
            {"run": run_id},
        )
    assert rows and len(rows) == count
    assert all(row["status"] == "verified" and row["version_id"] for row in rows)
    assert all(row["bucket"] == os.environ["AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET"] for row in rows)
    return digests, [dict(row) for row in rows]


def _read_originals(rows):
    fingerprints = {}
    with closing(fixture_original_reader()) as reader:
        for row in rows:
            key = (row["bucket"], row["object_key"], row["version_id"])
            assert key not in fingerprints
            response = reader.get_object(Bucket=key[0], Key=key[1], VersionId=key[2])
            assert response["VersionId"] == key[2]
            digest = hashlib.sha256()
            size = 0
            with closing(response["Body"]) as body:
                while chunk := body.read(1024 * 1024):
                    digest.update(chunk)
                    size += len(chunk)
            value = (size, digest.hexdigest())
            assert value == (row["size_bytes"], row["sha256"])
            fingerprints[key] = value
    return fingerprints


async def _snapshot(seed):
    catalog = await _catalog(seed, seed.run_id)
    rows, versions = await _rows_and_versions(seed.sessions.kw["bind"], seed.run_id)
    originals = await asyncio.to_thread(_read_originals, versions)
    return catalog, rows, versions, originals


async def _run_exit(seed, tmp_path, mode):
    # screenshot_seed is B, already seeded from a private offline cache and
    # private DuckDB. A gets a fresh root and the same validated seed inputs.
    run_a = uuid.uuid4()
    owner = seed.owner
    await owner.prepare_suite(run_a, "screenshot")
    process = None
    original_outcome = None
    failure = None
    try:
        await seed.seed_run(run_a)
        a_catalog = await _catalog(seed, run_a)
        before = await _snapshot(seed)
        assert {user["id"] for user in a_catalog["users"].values()}.isdisjoint(
            user["id"] for user in before[0]["users"].values()
        )
        script = tmp_path / f"screenshot-consumer-{mode}.mjs"
        script.write_text(CONSUMER, encoding="utf-8")
        process = await asyncio.create_subprocess_exec(
            "node", str(script), str(TRANSPORT), str(run_a), mode,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ready_line = await process.stdout.readline()
        if not ready_line:
            _, stderr = await process.communicate()
            raise RuntimeError(
                f"screenshot consumer exited before readiness with status {process.returncode}: "
                f"{stderr.decode('utf-8', errors='replace')}"
            )
        assert json.loads(ready_line) == {"event": "ready", "run_id": str(run_a)}
        if mode == "failure":
            process.stdin.write(b"fail\n")
            await process.stdin.drain()
        else:
            process.send_signal(signal.SIGINT)
        stdout, stderr = await process.communicate()
        output = stderr.decode("utf-8", errors="replace")
        original_outcome = RuntimeError(
            f"screenshot consumer {mode} exit={process.returncode}: {output}"
        )
        if mode == "failure":
            assert process.returncode == 1, (process.returncode, output)
            assert "seed/catalog failed: 422" in output, output
        else:
            assert process.returncode == 130, (process.returncode, output)
            assert "AbortError: screenshot consumer cancelled by SIGINT" in output, output
        assert not stdout, stdout.decode("utf-8", errors="replace")
    except BaseException as error:
        failure = error
        raise
    finally:
        cleanup_failure = None
        try:
            if process is not None and process.returncode is None:
                # A signal requests cooperative exit; only wait/owner cleanup
                # establishes the outcome, never the signal by itself.
                process.send_signal(signal.SIGINT)
                await process.communicate()
            response = await owner.post(
                "/api/v1/__test/seed/screenshot-cleanup", json={"run_id": str(run_a)}
            )
            assert response.status_code == 200, response.text
            async with seed.sessions.kw["bind"].connect() as db:
                state = await db.scalar(
                    text("SELECT state FROM yoao_fixture_v1.fixture_runs WHERE id=:run"),
                    {"run": run_a},
                )
                outstanding = await db.scalar(
                    text("SELECT count(*) FROM yoao_fixture_v1.fixture_run_resources "
                         "WHERE run_id=:run AND state <> 'retired'"),
                    {"run": run_a},
                )
            assert state == "retired" and outstanding == 0
        except BaseException as error:
            cleanup_failure = error
        if cleanup_failure is not None:
            errors = [error for error in (failure, original_outcome, cleanup_failure) if error is not None]
            if len(errors) > 1:
                raise builtins.BaseExceptionGroup("Screenshot consumer and exact owner cleanup failed", errors) from None
            raise cleanup_failure
    after = await _snapshot(seed)
    assert after[0] == before[0], "B screenshot catalog changed"
    assert after[1] == before[1], "B owned or dependent rows changed"
    assert after[2] == before[2], "B exact object version receipts changed"
    assert after[3] == before[3], "B exact object bytes changed"
    print(f"screenshot {mode}: A={run_a} B={seed.run_id} "
          f"consumer_exit={process.returncode} B_original_versions={len(after[2])}")


async def test_screenshot_consumer_failure_preserves_neighbor(screenshot_seed, tmp_path):
    await _run_exit(screenshot_seed, tmp_path, "failure")


async def test_screenshot_consumer_cancellation_preserves_neighbor(screenshot_seed, tmp_path):
    await _run_exit(screenshot_seed, tmp_path, "cancel")
