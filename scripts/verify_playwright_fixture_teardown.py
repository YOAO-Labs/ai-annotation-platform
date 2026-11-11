#!/usr/bin/env python3
"""Prove ordinary Playwright failure/SIGINT global teardown in an owned e2e session.

Run only inside one existing ``dev:worktree exec --mode e2e`` session. The
inner Playwright invocations intentionally fail; this command succeeds only
when both failures and both independent neighbors have been fully proved.
On an unproved result, source files, processes, owner state and evidence are
left in place for inspection; this utility never performs recovery cleanup.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps" / "web"
sys.path.insert(0, str(ROOT / "apps" / "api"))

from botocore.exceptions import ClientError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from fixture_native_playwright import _private, _start_ticks, _verify_run_receipts
from fixture_store import _client, _native_physical_retirement_receipt, _native_retirement_receipt, reader_credentials
from fixture_suite_coordinator import _verified_database
from linux_pidfd import open_pidfd, pidfd_alive, pidfd_send_signal
from verify_storage_candidates import Owner
from worktree_env import atomic_json, state_path

NATIVE = ("celery-media", "celery-pyramid", "media-worker", "image-worker", "proxy")
FAILURE = "INTENTIONAL_GLOBAL_TEARDOWN_ACCEPTANCE_FAILURE"


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def private_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def evidence_directory(path):
    path = Path(path).absolute()
    if not path.exists():
        path.mkdir(mode=0o700)
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700 and not any(path.iterdir())
            and path.resolve() == path, "evidence_directory_not_fresh_private")
    return path


def free_ports():
    # Holding all four sockets at once proves distinctness. Playwright's own
    # non-reuse/strict-port checks reject a race after these reservations close.
    sockets = []
    try:
        for _ in range(4):
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            sockets.append(listener)
        ports = [listener.getsockname()[1] for listener in sockets]
        require(len(set(ports)) == 4, "ports_not_distinct")
        return ports
    finally:
        for listener in sockets:
            listener.close()


def process_identity(pid, start=None):
    require(type(pid) is int and pid > 0, "process_identity_invalid")
    observed = _start_ticks(pid)
    if start is not None:
        require(observed == start, "process_start_changed")
    fd = open_pidfd(pid)
    try:
        require(pidfd_alive(fd) and _start_ticks(pid) == observed, "process_not_live")
    except BaseException:
        os.close(fd)
        raise
    return {"pid": pid, "start": observed, "fd": fd}


def live(process):
    return pidfd_alive(process["fd"]) and _start_ticks(process["pid"]) == process["start"]


def provider_child(owner_pid, run):
    children = Path(f"/proc/{owner_pid}/task/{owner_pid}/children").read_text().split()
    matches = []
    for raw_pid in children:
        pid = int(raw_pid)
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except FileNotFoundError:
            continue
        if args[-4:] == [b"-m", b"app.services.e2e_fixture_backend", str(run).encode(), b""]:
            matches.append(pid)
    require(len(matches) == 1, "provider_process_not_unique")
    return process_identity(matches[0])


def marker(path):
    return _private(path)


def safe_identity(process):
    return {"pid": process["pid"], "start": process["start"]}


def source_files(token):
    spec = WEB / "e2e" / "tests" / f"teardown-acceptance-{token}.spec.ts"
    config = WEB / f"teardown-acceptance-{token}.config.ts"
    hook = WEB / f"teardown-acceptance-{token}.hook.ts"
    spec_source = r'''import { test, expect } from "@playwright/test";
import { SeedAPI } from "../fixtures/seed";
import { closeFixtureTransport } from "../helpers/seed-owner-transport.mjs";
import { open, rename } from "node:fs/promises";
import { existsSync } from "node:fs";
import { join } from "node:path";
const dir = process.env.TEARDOWN_ACCEPTANCE_DIR!;
async function publish(name: string, value: object) {
  const tmp = join(dir, name + ".tmp");
  const f = await open(tmp, "wx", 0o600);
  try { await f.writeFile(JSON.stringify(value)); await f.sync(); } finally { await f.close(); }
  await rename(tmp, join(dir, name));
  const d = await open(dir, "r"); try { await d.sync(); } finally { await d.close(); }
}
async function wait(name: string) {
  while (!existsSync(join(dir, name))) await new Promise(r => setTimeout(r, 200));
}
test("real owned workbench", async ({ page, request }, info) => {
  test.setTimeout(0);
  const seed = new SeedAPI(request, info);
  let failure: unknown;
  try {
    const data = await seed.owned();
    const task = data.task_ids[0];
    await seed.advanceTask({ taskId: task, toStatus: "pending", annotatorEmail: data.annotator_email });
    await seed.injectToken(page, data.annotator_email);
    await page.goto(`/projects/${data.project_id}/annotate?task=${task}`);
    const stage = page.getByTestId("workbench-stage");
    await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
    await publish("ready.json", { suite_id: process.env.PLAYWRIGHT_FIXTURE_RUN_ID, project_id: data.project_id, task_id: task });
    await wait("release");
    if (process.env.TEARDOWN_ACCEPTANCE_ROLE === "failure") expect(false, "INTENTIONAL_GLOBAL_TEARDOWN_ACCEPTANCE_FAILURE").toBe(true);
    if (process.env.TEARDOWN_ACCEPTANCE_ROLE === "neighbor") {
      await page.reload();
      await expect(stage).toHaveAttribute("data-image-ready", "true", { timeout: 15_000 });
      await publish("reloaded.json", { suite_id: process.env.PLAYWRIGHT_FIXTURE_RUN_ID, project_id: data.project_id, task_id: task });
      await wait("finish");
    }
  } catch (error) {
    failure = error;
    throw error;
  } finally {
    try { await closeFixtureTransport(); }
    catch (error) {
      if (failure !== undefined) throw new AggregateError([failure, error], "Workbench and fixture transport failed");
      throw error;
    }
  }
});
'''
    hook_source = r'''import type { FullConfig } from "@playwright/test";
import { open, rename } from "node:fs/promises";
import { join } from "node:path";
import teardown from "./e2e/global-teardown";
async function publish(name: string) {
  const dir = process.env.TEARDOWN_ACCEPTANCE_DIR!;
  const tmp = join(dir, name + ".tmp");
  const f = await open(tmp, "wx", 0o600);
  try { await f.writeFile(JSON.stringify({ suite_id: process.env.PLAYWRIGHT_FIXTURE_RUN_ID, phase: name })); await f.sync(); } finally { await f.close(); }
  await rename(tmp, join(dir, name));
  const d = await open(dir, "r"); try { await d.sync(); } finally { await d.close(); }
}
export default async function observe(config: FullConfig) {
  await publish("teardown-entered.json");
  await teardown(config);
  await publish("teardown-returned.json");
}
'''
    config_source = f'''import {{ defineConfig }} from "@playwright/test";
import base from "./playwright.config";
export default defineConfig({{ ...base,
  testMatch: ["**/tests/{spec.name}"],
  globalTeardown: "./{hook.name}",
  workers: 1, retries: 0, reporter: "line",
  outputDir: process.env.TEARDOWN_ACCEPTANCE_OUTPUT!,
}});
'''
    for path, source in ((spec, spec_source), (hook, hook_source), (config, config_source)):
        private_file(path, source.encode())
    return [spec, hook, config]


def launch(case_dir, suite, role, ports, config):
    case_dir.mkdir(mode=0o700)
    env = dict(os.environ)
    env.update(PLAYWRIGHT_FIXTURE_RUN_ID=str(suite),
               PLAYWRIGHT_ISOLATED_API_PORT=str(ports[0]), PLAYWRIGHT_ISOLATED_WEB_PORT=str(ports[1]),
               TEARDOWN_ACCEPTANCE_DIR=str(case_dir), TEARDOWN_ACCEPTANCE_ROLE=role,
               TEARDOWN_ACCEPTANCE_OUTPUT=str(case_dir / "playwright-output"))
    # Execute Playwright in this exact node process, rather than signalling a
    # package-manager wrapper or a process group.
    log = os.open(case_dir / "playwright.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        process = subprocess.Popen(["node", str(WEB / "node_modules/@playwright/test/cli.js"), "test", "--config", str(config),
                                    "--project=chromium", "--workers=1", "--retries=0"],
                                   cwd=WEB, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
    finally:
        os.close(log)
    return {"dir": case_dir, "suite": suite, "process": process, "role": role,
            "playwright": process_identity(process.pid)}


async def wait_marker(instance, filename, neighbor=None):
    path = instance["dir"] / filename
    while not path.exists():
        require(instance["process"].poll() is None, "playwright_exited_before_" + filename)
        if neighbor is not None:
            check_live(neighbor)
        await asyncio.sleep(0.2)
    return marker(path)


def check_live(instance):
    require(instance["process"].poll() is None, "neighbor_playwright_exited")
    for process in instance["processes"].values():
        require(live(process), "neighbor_original_process_exited")
    native = _private(instance["native_path"])
    supervisor = _private(instance["directory"] / "native-supervisor.json")
    require(native["state"] == "ready" and supervisor["state"] == "serving",
            "neighbor_not_ready")
    require(all(native[name] == safe_identity(instance["processes"][name]) for name in NATIVE),
            "neighbor_native_incarnation_changed")
    require(supervisor["pid"] == instance["processes"]["supervisor"]["pid"]
            and supervisor["start"] == instance["processes"]["supervisor"]["start"]
            and supervisor["api"]["pid"] == instance["processes"]["api"]["pid"]
            and supervisor["api"]["start"] == instance["processes"]["api"]["start"]
            and supervisor["control"]["pid"] == instance["processes"]["control"]["pid"]
            and supervisor["control"]["start"] == instance["processes"]["control"]["start"],
            "neighbor_supervisor_incarnation_changed")
    require(_private(instance["directory"] / "generation.json") == instance["generation"],
            "neighbor_generation_changed")


async def query(engine, sql, parameters):
    async with engine.connect() as connection:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
        return (await connection.execute(text(sql), parameters)).mappings().all()


async def manifest(engine, run):
    rows = await query(engine,
        "SELECT to_jsonb(r) AS value FROM yoao_fixture_v1.fixture_run_resources r "
        "WHERE r.run_id=:run ORDER BY r.kind,r.resource_id", {"run": run})
    require(bool(rows), "fixture_manifest_empty")
    return [row["value"] for row in rows]


async def versions(engine, run):
    rows = await query(engine,
        "SELECT r.kind,c.id,c.location_id,c.storage_identity,c.bucket,c.object_key,c.version_id,"
        "c.sha256,c.size_bytes,c.status,c.receipt,c.verification,c.settled_at "
        "FROM yoao_fixture_v1.fixture_run_resources r JOIN public.managed_original_creations c "
        "ON c.id=r.resource_id WHERE r.run_id=:run AND r.kind IN ('creation','auxiliary') "
        "AND r.state='published' ORDER BY c.id", {"run": run})
    count = await query(engine, "SELECT count(*) AS n FROM yoao_fixture_v1.fixture_run_resources "
                        "WHERE run_id=:run AND kind IN ('creation','auxiliary') AND state='published'", {"run": run})
    require(rows and len(rows) == count[0]["n"] and all(
        r["status"] == "verified" and r["version_id"] and r["receipt"]
        and r["verification"] and r["settled_at"] for r in rows),
            "original_versions_unavailable")
    return [dict(row) for row in rows]


def read_originals(owner, rows):
    store = owner.data["fixture_store"]
    account = reader_credentials(owner, store)
    result = {}
    with closing(_client(store["admin_endpoint"], account["access_key"], account["secret_key"])) as reader:
        for row in rows:
            require(row["bucket"] == store["bucket"], "original_bucket_changed")
            key = (row["bucket"], row["object_key"], row["version_id"])
            require(key not in result, "original_version_duplicate")
            response = reader.get_object(Bucket=key[0], Key=key[1], VersionId=key[2])
            require(response["VersionId"] == key[2], "original_version_changed")
            digest = hashlib.sha256()
            size = 0
            with closing(response["Body"]) as body:
                while chunk := body.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            require((size, digest.hexdigest()) == (row["size_bytes"], row["sha256"]), "original_bytes_changed")
            result[key] = (size, digest.hexdigest())
    return result


async def snapshot(instance, resources, engine, owner, neighbor=None):
    ready = await wait_marker(instance, "ready.json", neighbor)
    require(ready["suite_id"] == str(instance["suite"]), "ready_suite_changed")
    rows = await query(engine, "SELECT id,state,family FROM yoao_fixture_v1.fixture_runs "
                       "WHERE parent_run_id=:suite ORDER BY id", {"suite": instance["suite"]})
    require(len(rows) == 1 and rows[0]["state"] == "open" and rows[0]["family"] == "owned", "suite_run_membership_changed")
    run = rows[0]["id"]
    task = await query(engine, "SELECT state FROM yoao_fixture_v1.fixture_run_resources "
                       "WHERE run_id=:run AND kind='task' AND resource_id=:task",
                       {"run": run, "task": uuid.UUID(ready["task_id"])})
    require(len(task) == 1 and task[0]["state"] == "published", "workbench_task_not_owned")
    directory = state_path(Path(resources["root"]), resources["mode"], "fixture-authority") / ("suite-" + instance["suite"].hex)
    native_path = directory / ("native-run-" + run.hex) / "owner.json"
    native = _private(native_path)
    supervisor = _private(directory / "native-supervisor.json")
    authority = _private(directory / "owner.json")
    generation = _private(directory / "generation.json")
    require(native["schema"] == "aap-fixture-native-run/v1" and native["run_id"] == str(run)
            and native["suite_id"] == str(instance["suite"]) and native["state"] == "ready"
            and supervisor["schema"] == "aap-fixture-native-supervisor/v1"
            and supervisor["suite_id"] == str(instance["suite"]) and supervisor["resources"] == resources
            and supervisor["state"] == "serving" and supervisor["api_owned"] is True
            and generation == {"resources": resources, "nonce": authority["nonce"], "state": "running"},
            "suite_owner_identity_changed")
    processes = {"supervisor": process_identity(supervisor["pid"], supervisor["start"]),
                 "api": process_identity(supervisor["api"]["pid"], supervisor["api"]["start"]),
                 "control": process_identity(supervisor["control"]["pid"], supervisor["control"]["start"]),
                 "authority": process_identity(authority["pid"], authority["start"])}
    for name in NATIVE:
        processes[name] = process_identity(native[name]["pid"], native[name]["start"])
    processes["provider"] = provider_child(authority["pid"], run)
    own_versions = await versions(engine, run)
    owned_manifest = await manifest(engine, run)
    originals = read_originals(owner, own_versions)
    instance.update(run=run, ready=ready, directory=directory, native_path=native_path,
                    processes=processes, generation=generation, native=native,
                    versions=own_versions, originals=originals, manifest=owned_manifest)
    private_file(instance["dir"] / "snapshot.json", json.dumps({
        "schema": "aap-playwright-global-teardown-snapshot/v1",
        "suite_id": str(instance["suite"]), "run_id": str(run),
        "workbench": ready, "native_receipt_sha256": native["receipt_sha256"],
        "generation_state": generation["state"],
        "playwright_process": safe_identity(instance["playwright"]),
        "processes": {name: safe_identity(process) for name, process in processes.items()},
        "original_versions": own_versions, "resource_manifest": owned_manifest,
    }, default=str, sort_keys=True).encode())
    check_live(instance)
    return instance


def compare_disposition(disposition, identity, flag):
    require(disposition["pid"] == identity["pid"] and disposition["start"] == identity["start"]
            and disposition["exit_code"] == 0 and disposition["pidfd_exit"] is True
            and disposition["descendants_drained"] is True and disposition[flag] is True,
            "supervisor_child_disposition_changed")


async def verify_retired(instance, resources, engine, owner_path):
    suite, run = instance["suite"], instance["run"]
    for name in ("teardown-entered.json", "teardown-returned.json"):
        require(marker(instance["dir"] / name) == {"suite_id": str(suite), "phase": name}, "teardown_hook_unproved")
    supervisor = _private(instance["directory"] / "native-supervisor.json")
    require(supervisor["schema"] == "aap-fixture-native-supervisor/v1" and supervisor["suite_id"] == str(suite)
            and supervisor["resources"] == resources and supervisor["state"] == "retired"
            and supervisor["pid"] == instance["processes"]["supervisor"]["pid"]
            and supervisor["start"] == instance["processes"]["supervisor"]["start"]
            and supervisor["native_run_ids"] == [str(run)] and supervisor["resolver_retired"] is True
            and supervisor["business_retired"] is True, "supervisor_retirement_unproved")
    compare_disposition(supervisor["api_disposition"], instance["processes"]["api"], "lifespan_shutdown")
    compare_disposition(supervisor["control_disposition"], instance["processes"]["control"], "settled_ack")
    require(not (instance["directory"] / "owner.json").exists()
            and _private(instance["directory"] / "generation.json") == {**instance["generation"], "state": "closed"},
            "authority_generation_not_closed")
    _verify_run_receipts(instance["directory"], suite, [str(run)])
    native = _private(instance["native_path"])
    require(native["receipt_sha256"] == instance["native"]["receipt_sha256"], "native_receipt_changed")
    for name in NATIVE:
        require(native[name] == safe_identity(instance["processes"][name])
                and native["child_dispositions"][name]["pid"] == instance["processes"][name]["pid"]
                and native["child_dispositions"][name]["start"] == instance["processes"][name]["start"],
                "native_original_disposition_changed")
    for process in instance["processes"].values():
        require(not pidfd_alive(process["fd"]), "original_process_not_exited")
    rows = await query(engine, "SELECT id,parent_run_id,state,family FROM yoao_fixture_v1.fixture_runs "
                       "WHERE id=:suite OR parent_run_id=:suite ORDER BY id", {"suite": suite})
    require(len(rows) == 2 and {r["id"] for r in rows} == {suite, run}
            and all(r["state"] == "retired" and r["family"] == "owned" for r in rows), "run_retirement_unproved")
    unsettled = await query(engine, "SELECT count(*) AS n FROM yoao_fixture_v1.fixture_run_resources "
                            "WHERE run_id=:run AND state<>'retired'", {"run": run})
    require(unsettled[0]["n"] == 0, "run_resources_unsettled")
    reclaimed = await query(engine,
        "SELECT c.id,c.version_id,c.sha256,c.size_bytes,cl.version_id AS claim_version,cl.state AS claim_state,"
        "cl.storage_identity,cl.bucket,cl.object_key,a.version_id AS attempt_version,a.expected_sha256,"
        "a.expected_size_bytes,a.status,a.outcome,a.physical_settled,a.absence_verified_at,"
        "t.creation_id AS retired_creation,t.bucket AS retired_bucket,t.object_key AS retired_key,"
        "t.storage_identity AS retired_identity,rr.state AS resource_state "
        "FROM yoao_fixture_v1.fixture_run_resources r JOIN public.managed_original_creations c ON c.id=r.resource_id "
        "JOIN public.managed_original_reclaim_claims cl ON cl.creation_id=c.id "
        "JOIN public.managed_original_reclaim_attempts a ON a.id=cl.current_attempt_id AND a.creation_id=c.id "
        "JOIN public.managed_original_retirements t ON t.id=a.retirement_id "
        "JOIN yoao_fixture_v1.fixture_run_resources rr ON rr.run_id=r.run_id AND rr.kind='reclaim' AND rr.resource_id=a.id "
        "WHERE r.run_id=:run AND r.kind IN ('creation','auxiliary') ORDER BY c.id", {"run": run})
    require(len(reclaimed) == len(instance["versions"]), "original_reclaim_membership_changed")
    for row, original in zip(reclaimed, instance["versions"], strict=True):
        require(row["id"] == original["id"] and row["version_id"] == row["claim_version"] == row["attempt_version"] == original["version_id"]
                and row["sha256"] == row["expected_sha256"] == original["sha256"]
                and row["size_bytes"] == row["expected_size_bytes"] == original["size_bytes"]
                and row["storage_identity"] == row["retired_identity"] == original["storage_identity"]
                and row["bucket"] == row["retired_bucket"] == original["bucket"]
                and row["object_key"] == row["retired_key"] == original["object_key"]
                and row["retired_creation"] == original["id"]
                and row["claim_state"] == "deleted" and row["status"] == "completed"
                and row["outcome"] in ("deleted", "already_absent") and row["physical_settled"] is True
                and row["absence_verified_at"] is not None and row["resource_state"] == "retired", "original_reclaim_unproved")
    # The scoped reader performs exact-version HEADs; neither an administrator
    # nor a writer identity is used to establish physical absence.
    owner = Owner(owner_path)
    store = owner.data["fixture_store"]
    account = reader_credentials(owner, store)
    with closing(_client(store["admin_endpoint"], account["access_key"], account["secret_key"])) as reader:
        for original in instance["versions"]:
            try:
                reader.head_object(Bucket=original["bucket"], Key=original["object_key"], VersionId=original["version_id"])
            except ClientError as error:
                require(error.response["ResponseMetadata"]["HTTPStatusCode"] == 404, "original_absence_unproved")
            else:
                raise RuntimeError("original_version_still_present")
    entry = owner.data["fixture_native_outputs"][run.hex]
    retirement = entry["retirement"]
    require(entry["state"] == "retired" and entry["run_id"] == str(run)
            and entry["receipt_sha256"] == native["receipt_sha256"], "native_output_retirement_unproved")
    _native_retirement_receipt(entry, run, native["target_proof"])
    physical = _native_physical_retirement_receipt(owner, run, native["target_proof"])
    digest = hashlib.sha256(json.dumps(physical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    require(retirement["physical_receipt"] == physical and retirement["physical_receipt_sha256"] == digest,
            "native_physical_receipt_unproved")
    business = owner.data["fixture_native_business"][suite.hex]
    require(business["state"] == "retired" and business["suite_id"] == str(suite)
            and business["roles_retired"] is True
            and set(business["iam_retirement"]) == {"control", "media-worker", "image-worker"}
            and all(business["accounts"][kind]["retired"] is True
                    and all(business["iam_retirement"][kind][key] is True
                            for key in ("user_submitted", "user_absent", "policy_submitted", "policy_absent"))
                    for kind in business["iam_retirement"]), "business_retirement_unproved")
    return {"suite_id": str(suite), "run_id": str(run), "native_receipt_sha256": native["receipt_sha256"],
            "physical_receipt_sha256": digest, "original_versions_reclaimed": len(reclaimed),
            "provider_exit": "pidfd observed; zero exit inferred from successful fixture provider close contract",
            "processes": {name: safe_identity(p) for name, p in instance["processes"].items()}}


async def run_case(name, evidence, sources, resources, engine, owner_path):
    case = evidence / name
    case.mkdir(mode=0o700)
    ports = free_ports()
    a_suite, b_suite = uuid.uuid4(), uuid.uuid4()
    b = launch(case / "B", b_suite, "neighbor", ports[2:], sources[2])
    owner = Owner(owner_path)
    await snapshot(b, resources, engine, owner)
    a = launch(case / "A", a_suite, name, ports[:2], sources[2])
    await snapshot(a, resources, engine, owner, b)
    require(a["run"] != b["run"] and a_suite != b_suite, "suite_identity_collision")
    check_live(b)
    if name == "failure":
        private_file(a["dir"] / "release", b"release\n")
    else:
        require(live(a["playwright"]), "playwright_identity_changed")
        pidfd_send_signal(a["playwright"]["fd"], signal.SIGINT)
    while a["process"].poll() is None:
        check_live(b)
        await asyncio.sleep(0.2)
    a_status = a["process"].returncode
    atomic_json(a["dir"] / "wait-status.json", {"pid": a["playwright"]["pid"], "returncode": a_status}, exclusive=True)
    require(a_status != 0, "a_unexpected_success")
    if name == "failure":
        require(FAILURE in (a["dir"] / "playwright.log").read_text(errors="replace"), "intentional_assertion_not_observed")
    a_proof = await verify_retired(a, resources, engine, owner_path)
    check_live(b)
    neighbor_run = await query(engine, "SELECT id,state,family FROM yoao_fixture_v1.fixture_runs "
                               "WHERE parent_run_id=:suite", {"suite": b_suite})
    require(len(neighbor_run) == 1 and neighbor_run[0]["id"] == b["run"]
            and neighbor_run[0]["state"] == "open" and neighbor_run[0]["family"] == "owned",
            "neighbor_run_changed")
    require(await manifest(engine, b["run"]) == b["manifest"], "neighbor_resource_manifest_changed")
    after_versions = await versions(engine, b["run"])
    require(after_versions == b["versions"] and read_originals(Owner(owner_path), after_versions) == b["originals"],
            "neighbor_original_versions_changed")
    private_file(b["dir"] / "release", b"reload\n")
    reloaded = await wait_marker(b, "reloaded.json")
    require(reloaded == b["ready"], "neighbor_reload_identity_changed")
    check_live(b)
    private_file(b["dir"] / "finish", b"finish\n")
    while b["process"].poll() is None:
        await asyncio.sleep(0.2)
    b_status = b["process"].returncode
    atomic_json(b["dir"] / "wait-status.json", {"pid": b["playwright"]["pid"], "returncode": b_status}, exclusive=True)
    require(b_status == 0, "neighbor_playwright_failed")
    b_proof = await verify_retired(b, resources, engine, owner_path)
    summary = {"schema": "aap-playwright-global-teardown-acceptance/v1", "case": name,
               "a_wait_status": a_status, "b_wait_status": b_status, "a": a_proof, "b": b_proof,
               "neighbor_original_versions_preserved": len(after_versions), "neighbor_real_reload": True}
    atomic_json(case / "summary.json", summary, exclusive=True)
    for instance in (a, b):
        for process in [instance["playwright"], *instance["processes"].values()]:
            os.close(process["fd"])
    print(json.dumps({"case": name, "a_wait_status": a_status, "b_wait_status": b_status,
                      "a_suite": str(a_suite), "a_run": str(a["run"]),
                      "b_suite": str(b_suite), "b_run": str(b["run"])}), flush=True)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True)
    args = parser.parse_args()
    evidence = evidence_directory(args.evidence)
    require(os.environ.get("AAP_WORKTREE_MODE") == "e2e", "e2e_exec_required")
    require(not os.environ.get("CI") and not os.environ.get("PLAYWRIGHT_RASTER_OFFLINE")
            and not os.environ.get("PLAYWRIGHT_RASTER_MASK_MATRIX")
            and not os.environ.get("AAP_PRIVATE_FIXTURE_RASTER_OWNER")
            and not os.environ.get("AAP_PRIVATE_FIXTURE_AUTHORITY_NATIVE"), "ordinary_playwright_required")
    owner_url, resources, identity = await _verified_database(dict(os.environ))
    require(resources["mode"] == "e2e" and Path(resources["root"]).resolve() == ROOT,
            "worktree_identity_changed")
    owner_path = state_path(ROOT, "e2e", "fixture-store-owner.json")
    owner = Owner(owner_path)
    require(owner.data["fixture_store"]["database"] == identity, "fixture_store_database_changed")
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    sources = source_files(uuid.uuid4().hex)
    try:
        await run_case("failure", evidence, sources, resources, engine, owner_path)
        await run_case("cancel", evidence, sources, resources, engine, owner_path)
    finally:
        await engine.dispose()
    for path in sources:
        path.unlink()
    atomic_json(evidence / "complete.json", {"schema": "aap-playwright-global-teardown-acceptance/v1", "cases": ["failure", "cancel"]}, exclusive=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        # Logs and all paths remain private; do not print exception text, DSNs,
        # credentials, or attempt cleanup after an unproved outcome.
        reason = str(error) if type(error) is RuntimeError and re.fullmatch(r"[a-z][a-z0-9_]*", str(error)) else type(error).__name__
        print(f"Global teardown acceptance unproved: {reason}", file=sys.stderr)
        raise SystemExit(1) from None
