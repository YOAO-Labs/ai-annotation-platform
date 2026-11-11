#!/usr/bin/env python3
"""Prove real screenshot failure/cancellation retirement beside a live sibling.

Run only inside an already-owned e2e worktree exec.  A failed proof deliberately
leaves its processes and private evidence in place for the worktree owner.
This exercises the real screenshot hooks and capture, not a production scene's
failure handler or cancellation of an in-flight original read.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web"
API = ROOT / "apps/api"
sys.path.insert(0, str(API))
sys.path.insert(0, str(ROOT / "scripts"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from fixture_suite_coordinator import _verified_database
from fixture_store import reader_credentials, verified_store
from linux_pidfd import open_pidfd, pidfd_alive, pidfd_send_signal
from verify_storage_candidates import Owner
from worktree_env import state_path
from tests.test_screenshot_exit_live import _read_originals, _rows_and_versions

CHILDREN = ("celery-media", "celery-pyramid", "media-worker", "image-worker", "proxy")
IAM = {f"{role}:{kind}" for role in ("writer", "reader", "cleanup") for kind in ("user", "policy")}

CONFIG = r'''import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from '@playwright/test';
import base from '../playwright.screenshots.config';
const here = path.dirname(fileURLToPath(import.meta.url));
const servers = [...base.webServer!];
servers[0] = { ...servers[0], command: `node ${path.join(here, 'launcher.mjs')}` };
export default defineConfig({
  ...base, testDir: here, testMatch: '**/t2.spec.ts', workers: 1, retries: 0,
  timeout: 900_000, globalSetup: path.resolve(here, '../e2e/screenshots/global-setup.ts'),
  globalTeardown: path.join(here, 'teardown.ts'), outputDir: path.join(here, 'results'),
  reporter: [['list']], webServer: servers,
  projects: base.projects!.map(p => ({ ...p, testMatch: p.name === 'desktop-light'
    ? ['**/t2.spec.ts'] : ['**/no-such-t2-spec.ts'] })),
});
'''
TEARDOWN = r'''import { openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import path from 'node:path';
import type { FullConfig } from '@playwright/test';
import realTeardown from '../e2e/screenshots/global-teardown';
function mark(event: string) {
  const f = openSync(path.join(process.env.T2_EVIDENCE!, `${process.env.SCREENSHOT_FIXTURE_RUN_ID}.teardown`), 'a', 0o600);
  try { writeSync(f, `${event}\n`); fsyncSync(f); } finally { closeSync(f); }
}
export default async function teardown(config: FullConfig): Promise<void> {
  mark('entry'); await realTeardown(config); mark('returned');
}
'''
LAUNCHER = r'''import { spawn } from 'node:child_process';
import { openSync, writeSync, fsyncSync, closeSync } from 'node:fs';
import path from 'node:path';
const child = spawn(process.execPath, ['scripts/run-screenshot-native.mjs'], { stdio: 'inherit' });
// Playwright signals its web-server process group; the actual launcher receives it too.
process.on('SIGTERM', () => {}); process.on('SIGINT', () => {});
child.on('error', error => { console.error(error.name); process.exitCode = 1; });
child.on('close', (code, signal) => {
  const f = openSync(path.join(process.env.T2_EVIDENCE, `${process.env.SCREENSHOT_FIXTURE_RUN_ID}.launcher`), 'wx', 0o600);
  try { writeSync(f, JSON.stringify({pid: child.pid, code, signal})); fsyncSync(f); } finally { closeSync(f); }
  process.exitCode = code === 0 && signal === null ? 0 : 1;
});
'''
SPEC = r'''import { openSync, writeSync, fsyncSync, closeSync, linkSync, unlinkSync } from 'node:fs';
import path from 'node:path';
import { test, expect } from '../e2e/fixtures/seed';
import { loadScreenshotCatalog } from '../e2e/screenshots/catalog-runtime';
test('real screenshot consumer', async ({ page, seed }) => {
  const run = process.env.SCREENSHOT_FIXTURE_RUN_ID!;
  const catalog = loadScreenshotCatalog();
  if (catalog.run_id !== run) throw new Error('catalog run mismatch');
  await seed.injectToken(page, catalog.users.admin.email);
  const project = catalog.projects.image_demo;
  const response = await page.goto(`/projects/${project.id}/annotate?task=${project.tasks.annotating.id}`);
  if (!response?.ok()) throw new Error(`image task status ${response?.status()}`);
  await expect(page.getByTestId('workbench-stage')).toHaveAttribute('data-image-ready', 'true', {timeout: 60_000});
  const png = await page.screenshot({ type: 'png', animations: 'disabled' });
  if (png.length < 33 || !png.subarray(0, 8).equals(Buffer.from('89504e470d0a1a0a', 'hex')) ||
      png.readUInt32BE(8) !== 13 || png.toString('ascii', 12, 16) !== 'IHDR' ||
      png.readUInt32BE(16) !== 1440 || png.readUInt32BE(20) !== 900)
    throw new Error('invalid screenshot PNG signature or dimensions');
  const release = new Promise<void>(resolve => process.once('SIGUSR1', () => resolve()));
  const file = path.join(process.env.T2_EVIDENCE!, `${run}.ready`);
  const temporary = `${file}.${process.pid}.tmp`;
  const fd = openSync(temporary, 'wx', 0o600);
  try { writeSync(fd, JSON.stringify({run, pid: process.pid, width: 1440, height: 900})); fsyncSync(fd); }
  finally { closeSync(fd); }
  linkSync(temporary, file); unlinkSync(temporary);
  const dir = openSync(process.env.T2_EVIDENCE!, 'r'); try { fsyncSync(dir); } finally { closeSync(dir); }
  await release;
  try { await page.screenshot({type: 'png', path: '/dev/full', animations: 'disabled'}); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code !== 'ENOSPC' && !String(error).includes('ENOSPC')) throw error;
    const marker = openSync(path.join(process.env.T2_EVIDENCE!, `${run}.enospc`), 'wx', 0o600);
    try { writeSync(marker, JSON.stringify({run, code: 'ENOSPC'})); fsyncSync(marker); }
    finally { closeSync(marker); }
    console.error('T2_CAPTURE_ENOSPC'); throw error;
  }
  throw new Error('expected /dev/full screenshot write to fail');
});
'''
CATALOG = r'''import { writeFileSync } from 'node:fs';
import { getScreenshotCatalog, closeFixtureTransport } from '../e2e/helpers/seed-owner-transport.mjs';
const run = process.argv[2], dest = process.argv[3];
try {
  const response = await getScreenshotCatalog(`http://fixture.local/api/v1/__test/seed/catalog?profile=screenshots&backend_requirements=none&run_id=${run}`);
  if (!response.ok()) throw new Error(`catalog status ${response.status()}`);
  const catalog = await response.json();
  if (catalog.run_id !== run || !Array.isArray(catalog.backend_requirements) || catalog.backend_requirements.length) throw new Error('catalog identity changed');
  writeFileSync(dest, JSON.stringify(catalog), {mode: 0o600, flag: 'wx'});
} finally { await closeFixtureTransport(); }
'''
BROWSER = r'''import { chromium } from '@playwright/test';
import { existsSync, readFileSync } from 'node:fs';
import { getScreenshotCatalog, postFixture, closeFixtureTransport } from '../e2e/helpers/seed-owner-transport.mjs';
const run = process.argv[2], base = process.argv[3];
let catalog, login;
try {
  const response = await getScreenshotCatalog(`http://fixture.local/api/v1/__test/seed/catalog?profile=screenshots&backend_requirements=none&run_id=${run}`);
  if (!response.ok()) throw new Error('B catalog failed');
  catalog = await response.json();
  if (catalog.run_id !== run || catalog.backend_requirements.length) throw new Error('B catalog mismatch');
  const auth = await postFixture('http://fixture.local/api/v1/__test/seed/login', {data: {email: catalog.users.admin.email}});
  if (!auth.ok()) throw new Error('B login failed');
  login = await auth.json();
} finally { await closeFixtureTransport(); }
const safe = ['PATH','HOME','TMPDIR','LANG','LC_ALL','LC_CTYPE','TZ','XDG_RUNTIME_DIR','XDG_CACHE_HOME','FONTCONFIG_PATH','DBUS_SESSION_BUS_ADDRESS','LD_LIBRARY_PATH'];
const env = Object.fromEntries(safe.filter(k => process.env[k] !== undefined).map(k => [k, process.env[k]]));
const browser = await chromium.launch({env, args: ['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--ignore-gpu-blocklist','--disable-accelerated-2d-canvas']});
const page = await browser.newPage({viewport: {width: 1440, height: 900}, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce'});
await page.goto(base);
await page.evaluate(({token, user}) => { localStorage.setItem('token', token); localStorage.setItem('auth-storage', JSON.stringify({state: {token, user}, version: 0})); }, {token: login.access_token, user: login.user});
const project = catalog.projects.image_demo;
const response = await page.goto(`${base}/projects/${project.id}/annotate?task=${project.tasks.annotating.id}`);
if (!response?.ok()) throw new Error('B task navigation failed');
async function ready() { await page.getByTestId('workbench-stage').waitFor({state: 'visible', timeout: 60_000}); await page.waitForFunction(() => document.querySelector('[data-testid="workbench-stage"]')?.getAttribute('data-image-ready') === 'true', null, {timeout: 60_000}); }
await ready(); console.log('T2_B_READY');
let sequence = 0;
while (true) {
  const command = `${process.env.T2_EVIDENCE}/b-command-${++sequence}.json`;
  while (!existsSync(command)) await new Promise(resolve => setTimeout(resolve, 200));
  const line = JSON.parse(readFileSync(command, 'utf8'));
  if (line === 'close') break;
  if (!/^reload-[12]$/.test(line)) throw new Error('invalid B browser command');
  const result = await page.reload(); if (!result?.ok()) throw new Error('B reload failed');
  await ready(); console.log(`T2_B_${line}`);
}
await browser.close(); console.log('T2_B_CLOSED');
'''
CLOSED = r'''import { requireScreenshotCaptureClosed } from '../e2e/screenshots/fixture-run.mjs';
requireScreenshotCaptureClosed(process.argv[2]);
'''


class ProofError(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise ProofError(message)


def private_read(path):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        actual, named = os.fstat(stream.fileno()), path.lstat()
        require(stat.S_ISREG(actual.st_mode) and actual.st_uid == os.geteuid()
                and stat.S_IMODE(actual.st_mode) == 0o600 and actual.st_nlink == 1
                and (actual.st_dev, actual.st_ino) == (named.st_dev, named.st_ino), "private file identity changed")
        return stream.read()


def record(path):
    return json.loads(private_read(path))


def save(path, value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    path = Path(path)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temporary, path, follow_symlinks=False)
    temporary.unlink()
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def private_dir(path):
    info = Path(path).lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "private directory identity changed")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def ticks(pid):
    value = Path(f"/proc/{pid}/stat").read_text()
    return int(value[value.rfind(")") + 2:].split()[19])


class Pin:
    def __init__(self, pid, start=None):
        require(type(pid) is int and pid > 1, "invalid process identity")
        self.pid = pid
        self.start = ticks(pid) if start is None else start
        self.fd = open_pidfd(pid)
        require(pidfd_alive(self.fd) and ticks(pid) == self.start, "process identity changed")

    def alive(self):
        return pidfd_alive(self.fd) and ticks(self.pid) == self.start

    def send(self, sig):
        require(self.alive(), "signal target identity changed")
        pidfd_send_signal(self.fd, sig)

    def exited(self):
        require(not pidfd_alive(self.fd), "recorded process has not exited")


def descendants(parent):
    parents = {}
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            value = (path / 'stat').read_text()
            parents[int(path.name)] = int(value[value.rfind(')') + 2:].split()[1])
        except (FileNotFoundError, ProcessLookupError):
            continue
    found = set(); frontier = {parent}
    while frontier:
        frontier = {pid for pid, ppid in parents.items() if ppid in frontier} - found
        found.update(frontier)
    return found


def cmdline(pid):
    return Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')


def find_child(parent, needle, *, browser=False):
    found = []
    for pid in descendants(parent):
        try:
            args = cmdline(pid)
            if browser:
                match = bool(args and args[0].rsplit(b'/', 1)[-1] in
                             (b'chrome', b'chromium', b'chrome-headless-shell')
                             and not any(arg.startswith(b'--type=') for arg in args))
            else:
                match = any(needle in arg for arg in args)
            if match:
                found.append(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
    require(len(found) == 1, "expected exact child process was not observed")
    return Pin(found[0])


def free_ports(count):
    reservations = []
    for _ in range(count):
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); reservations.append(sock)
    ports = [sock.getsockname()[1] for sock in reservations]
    require(len(set(ports)) == count, "port selection collision")
    for sock in reservations:
        sock.close()
    return ports


def require_free(port):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', port))


def wait_for(predicate, processes, label, timeout=900):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            require(process.poll() is None, f"{label}: required process exited")
        if predicate():
            return
        time.sleep(0.2)
    raise ProofError(f"{label}: timed out; resources retained")


def health(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/health/db', timeout=5) as response:
            return response.status == 200
    except OSError:
        return False


def listener_inode(port):
    inode_set = set()
    for line in Path('/proc/net/tcp').read_text().splitlines()[1:]:
        fields = line.split()
        if fields[1] == f'0100007F:{port:04X}' and fields[3] == '0A':
            inode_set.add(fields[9])
    require(len(inode_set) == 1, 'loopback listener identity is ambiguous')
    return inode_set.pop()


def process_sockets(pid):
    sockets = set()
    for fd in Path(f'/proc/{pid}/fd').iterdir():
        try:
            target = os.readlink(fd)
        except FileNotFoundError:
            continue
        if target.startswith('socket:[') and target.endswith(']'):
            sockets.add(target[8:-1])
    return sockets


def listener_owned(port, pin):
    require(pin.alive() and listener_inode(port) in process_sockets(pin.pid) and pin.alive(),
            'loopback listener belongs to another process')


def find_listener(parent, port):
    inode = listener_inode(port)
    found = []
    for pid in descendants(parent):
        try:
            if inode in process_sockets(pid):
                found.append(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue
    require(len(found) == 1, 'owned web listener was not uniquely observed')
    result = Pin(found[0])
    listener_owned(port, result)
    return result


LOGGER = r'''import re, sys
for raw in sys.stdin.buffer:
    line = raw.decode('utf-8', errors='replace').strip()
    if re.fullmatch(r'Owned screenshot API: http://127\.0\.0\.1:[0-9]+; run: [0-9a-f-]{36}', line):
        print(line, flush=True)
    for marker in ('T2_B_READY', 'T2_B_reload-1', 'T2_B_reload-2', 'T2_B_CLOSED', 'T2_CAPTURE_ENOSPC'):
        if marker in line:
            print(marker, flush=True)
    if 'ENOSPC' in line:
        print('ENOSPC', flush=True)
'''


class Child:
    def __init__(self, argv, *, cwd, env, log):
        self.log = log
        output = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            # This reader outlives the verifier on failure, avoiding EPIPE in
            # retained children while the owner investigates.
            self.logger = subprocess.Popen(
                [sys.executable, '-c', LOGGER], stdin=subprocess.PIPE,
                stdout=output, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            self.process = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=self.logger.stdin, stderr=subprocess.STDOUT, start_new_session=True,
            )
            self.logger.stdin.close()
        finally:
            os.close(output)
        self.pin = Pin(self.process.pid)

    def has(self, text):
        return text in private_read(self.log).decode('utf-8', errors='replace')

    def wait(self, timeout=900):
        result = self.process.wait(timeout=timeout)
        require(self.logger.wait(timeout=10) == 0, "child output did not settle")
        return result


@contextmanager
def reader_environment(store, reader):
    values = {'AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT': store['endpoint'],
              'AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET': store['bucket'],
              'AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY': reader['access_key'],
              'AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY': reader['secret_key']}
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None: os.environ.pop(key, None)
            else: os.environ[key] = value


def load_store(root, resources, identity):
    manifest = state_path(root, 'e2e', 'fixture-store-owner.json')
    lock = state_path(root, 'e2e', 'fixture-store.lock')
    fd = os.open(lock, os.O_RDWR | os.O_NOFOLLOW)
    try:
        info, current = os.fstat(fd), lock.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink == 1 and (info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), 'store lock changed')
        fcntl.flock(fd, fcntl.LOCK_SH)
        private_read(manifest)
        owner = Owner(manifest)
        store, _ = verified_store(owner, identity)
        require(store['state'] == 'published' and resources['root'] == str(root), 'fixture store not published')
        reader = reader_credentials(owner, store)
        return store, reader
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN); os.close(fd)


def records(root, run):
    authority = root / '.worktree/e2e/fixture-authority'
    suite = authority / ('suite-' + run.hex)
    private_dir(authority); private_dir(suite); private_dir(suite / ('native-run-' + run.hex))
    return (record(suite / 'native-supervisor.json'), record(suite / ('native-run-' + run.hex) / 'owner.json'),
            record(root / '.worktree/e2e/fixture-store-owner.json'))


def pin_native(supervisor, journal):
    return {name: Pin(value['pid'], value['start']) for name, value in
            {'api': supervisor['api'], **{name: journal[name] for name in CHILDREN}}.items()}


def native_identity(root, resources, run, port):
    supervisor, journal, owner = records(root, run)
    authority = record(root / '.worktree/e2e/fixture-authority/owner.json')
    generation = record(root / '.worktree/e2e/fixture-authority/generation.json')
    require(generation == {'resources': resources, 'nonce': authority['nonce'], 'state': 'running'}, 'shared authority generation changed')
    require(supervisor['schema'] == 'aap-fixture-native-supervisor/v1'
            and supervisor['suite_id'] == str(run) and supervisor['root_origin'] == {'family': 'screenshot', 'parent_run_id': None}
            and supervisor['api_owned'] is True and supervisor['api']['port'] == port
            and supervisor['resources'] == resources and supervisor['authority_nonce'] == authority['nonce']
            and (supervisor['pid'], supervisor['start']) == (authority['pid'], authority['start'])
            and str(uuid.UUID(supervisor['birth_admission_id'])) == supervisor['birth_admission_id'], 'native supervisor identity changed')
    require(journal['schema'] == 'aap-fixture-native-run/v1' and journal['suite_id'] == str(run)
            and journal['run_id'] == str(run) and journal['state'] == 'ready', 'native run not ready')
    target = owner['fixture_native_outputs'][run.hex]
    require(target['state'] == 'ready' and target['run_id'] == str(run)
            and journal['receipt_sha256'] == target['receipt_sha256'], 'native target identity changed')
    identity = {'supervisor': {key: supervisor[key] for key in ('suite_id','resources','pid','start','authority_nonce','root_origin','birth_admission_id','api_owned','api')},
                'journal': {key: journal[key] for key in ('run_id','suite_id','state','receipt_sha256','proxy_origin',*CHILDREN)},
                'target': {key: target[key] for key in ('state','run_id','storage_identity','bucket','receipt_sha256')},
                'provider': {'owner_nonce': owner['nonce'], **{key: owner['fixture_store'][key] for key in ('container_id','network_id','volume_id','bucket')}},
                'authority': {key: authority[key] for key in ('nonce','pid','start','resources')}}
    return identity, supervisor, journal, owner


async def db_identities(engine, run):
    async with engine.connect() as db:
        return [await db.scalar(text('SELECT aap_assets_v1.fixture_native_reader_identity(:run)'), {'run': run}),
                await db.scalar(text('SELECT aap_jobs_v1.fixture_media_executor_identity(:run)'), {'run': run})]


async def snapshot(engine, root, resources, run, port, store, reader, catalog):
    identity, supervisor, journal, _ = native_identity(root, resources, run, port)
    for pin in [Pin(supervisor['pid'], supervisor['start']), *pin_native(supervisor, journal).values()]:
        require(pin.alive(), 'live sibling process changed'); os.close(pin.fd)
    require(health(port), 'live sibling API unhealthy')
    with reader_environment(store, reader):
        rows, versions = await _rows_and_versions(engine, run)
        originals = _read_originals(versions)
    principals = await db_identities(engine, run)
    require(all(isinstance(item, dict) and item.get('state') == 'active' for item in principals), 'live sibling roles changed')
    return {'catalog': catalog, 'rows': rows, 'versions': versions, 'originals': sorted((list(key), list(value)) for key, value in originals.items()), 'native': identity, 'principals': principals}


def invoke_node(script, arguments, env, cwd, evidence, name):
    child = Child(['node', str(script), *map(str, arguments)], cwd=cwd, env=env, log=evidence / (name + '.log'))
    require(child.wait() == 0, f'{name} failed; see private evidence')


def catalog_for(temp, evidence, env, run, name):
    filename = evidence / (name + '.catalog.json')
    invoke_node(temp / 'catalog.mjs', [run, filename], env, WEB, evidence, name + '-catalog')
    return record(filename)


def target_receipt(owner, run, journal, birth):
    target = owner['fixture_native_outputs'][run.hex]; retirement = target['retirement']
    proof = journal['target_proof']; physical = retirement['physical_receipt']
    require(target['state'] == 'retired' and target['run_id'] == str(run)
            and target['receipt_sha256'] == birth['receipt_sha256'] and target['bucket'] == birth['bucket']
            and target['storage_identity'] == birth['storage_identity'] and retirement['native_receipt'] == proof
            and proof['run_id'] == str(run) and proof['storage_identity'] == birth['storage_identity']
            and proof['target_receipt_sha256'] == birth['receipt_sha256'] and retirement['bucket'] == 'absent'
            and retirement['iam'] == dict.fromkeys(IAM, 'absent'), 'native target retirement changed')
    require(physical['schema'] == 'aap-fixture-native-target-physical-retirement/v1'
            and physical['native_receipt'] == proof and physical['bucket'] == birth['bucket']
            and physical['provider'] == birth['provider'] and physical['accounts'] == birth['accounts']
            and physical['bucket_absent'] is True and physical['iam_absent'] == dict.fromkeys(IAM, True)
            and digest(physical) == retirement['physical_receipt_sha256'], 'native physical retirement unproven')
    return proof, physical, retirement['physical_receipt_sha256']


def birth_receipt(owner, run):
    path = Path(owner['private_directory']) / ('native-output-' + run.hex + '-receipt.json')
    raw = private_read(path); birth = json.loads(raw); target = owner['fixture_native_outputs'][run.hex]
    require(hashlib.sha256(raw).hexdigest() == target['receipt_sha256'] and birth['run_id'] == str(run)
            and birth['bucket'] == target['bucket'] and birth['storage_identity'] == target['storage_identity']
            and birth['provider'] == {'owner_nonce': owner['nonce'], **{key: owner['fixture_store'][key]
                                         for key in ('container_id', 'network_id', 'volume_id')}},
            'native birth receipt changed')
    return {**birth, 'receipt_sha256': target['receipt_sha256']}


async def verify_retired(engine, root, resources, run, initial, pins, versions, birth, principals, temp, evidence, env, label):
    invoke_node(temp / 'closed.mjs', [run], env, WEB, evidence, label + '-closed')
    supervisor, journal, owner = records(root, run)
    require(supervisor['state'] == 'retired' and supervisor['native_run_ids'] == [str(run)]
            and supervisor['api'] == initial['supervisor']['api'] and supervisor['resources'] == resources
            and all(supervisor[key] == initial['supervisor'][key] for key in
                    ('pid', 'start', 'root_origin', 'api_owned', 'authority_nonce', 'birth_admission_id')),
            'retired supervisor identity changed')
    require(journal['state'] == 'retired' and journal['suite_id'] == str(run) and journal['run_id'] == str(run)
            and all(journal[key] == initial['journal'][key] for key in ('receipt_sha256', 'proxy_origin'))
            and set(journal['drained']) == set(CHILDREN) and len(journal['drained']) == len(CHILDREN)
            and set(journal['child_dispositions']) == set(CHILDREN), 'native child retirement missing')
    for name in CHILDREN:
        original = initial['journal'][name]; disposition = journal['child_dispositions'][name]
        require(journal[name] == original and all(disposition[key] == original[key] for key in ('pid','start'))
                and disposition['exit_code'] == 0 and disposition['pidfd_exit'] is True
                and disposition['descendants_drained'] is True
                and disposition['acknowledgment'] == ('warm_exit' if name.startswith('celery-') else 'settled'), 'native child disposition changed')
    for pin in pins.values(): pin.exited()
    proof, physical, physical_sha = target_receipt(owner, run, journal, birth)
    reclaim_receipts = []
    async with engine.connect() as db:
        run_row = (await db.execute(text('SELECT family,parent_run_id,state FROM yoao_fixture_v1.fixture_runs WHERE id=:run'), {'run': run})).one()
        require(tuple(run_row) == ('screenshot', None, 'retired'), 'fixture run not retired')
        for table, state in (('fixture_run_resources', 'retired'), ('fixture_run_admissions', 'settled')):
            count = await db.scalar(text(f"SELECT count(*) FROM yoao_fixture_v1.{table} WHERE run_id=:run AND state<>:state"), {'run': run, 'state': state})
            require(count == 0, 'fixture membership not settled')
        reader, executor = await db_identities(engine, run)
        require(all(isinstance(item, dict) and item.get('state') == 'retired' for item in (reader, executor)), 'fixture native principals not retired')
        require(all(before.get('state') == 'active'
                    and {k: v for k, v in before.items() if k != 'state'}
                    == {k: v for k, v in after.items() if k != 'state'}
                    for before, after in zip(principals, (reader, executor), strict=True)),
                'fixture principal identity changed at retirement')
        require(reader['name'] == 'aap_fixture_media_read_' + run.hex, 'reader role identity changed')
        require(not await db.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=:name)'), {'name': reader['name']}), 'reader role remains')
        require(not await db.scalar(text('SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_stat_activity WHERE usesysid=:oid)'), {'oid': reader['oid']}), 'reader session remains')
        target = await db.scalar(text('SELECT aap_assets_v1.fixture_native_target_receipt(:run)'), {'run': run})
        ack = (await db.execute(text('SELECT receipt,receipt_sha256 FROM aap_assets_v1.fixture_native_target_physical_acknowledgements WHERE run_id=:run'), {'run': run})).one()
        require(target == proof and json.loads(ack.receipt) == physical and ack.receipt_sha256 == physical_sha, 'database target retirement not acknowledged')
        for row in versions:
            values = (await db.execute(text('''SELECT r.id AS retirement_id,r.creation_id,r.bucket,r.object_key,r.storage_identity,
                    c.creation_id AS claim_creation,c.version_id AS claim_version,c.storage_identity AS claim_storage,c.state AS claim_state,
                    a.id AS attempt_id,a.creation_id AS attempt_creation,a.retirement_id AS attempt_retirement,a.version_id AS attempt_version,
                    a.status,a.physical_settled,a.absence_verified_at,fr.state AS resource_state
                FROM public.managed_original_retirements r
                JOIN public.managed_original_reclaim_claims c ON c.bucket=r.bucket AND c.object_key=r.object_key
                JOIN public.managed_original_reclaim_attempts a ON a.id=c.current_attempt_id
                JOIN yoao_fixture_v1.fixture_run_resources fr ON fr.run_id=:run AND fr.kind='reclaim' AND fr.resource_id=a.id
                WHERE r.creation_id=:creation'''), {'run': run, 'creation': row['id']})).mappings().all()
            require(len(values) == 1, 'original reclaim ownership missing')
            v = values[0]
            require(v['creation_id'] == v['claim_creation'] == v['attempt_creation'] == row['id']
                    and v['bucket'] == row['bucket'] and v['object_key'] == row['object_key']
                    and v['claim_version'] == v['attempt_version'] == row['version_id']
                    and v['claim_storage'] == v['storage_identity'] and v['attempt_retirement'] == v['retirement_id']
                    and v['claim_state'] == 'deleted' and v['status'] == 'completed' and v['physical_settled'] is True
                    and v['absence_verified_at'] is not None and v['resource_state'] == 'retired', 'original physical reclaim unproven')
            reclaim_receipts.append(dict(v))
    save(evidence / (label + '-retirement.json'), {'supervisor': supervisor, 'journal': journal,
         'physical': physical, 'physical_sha256': physical_sha, 'principals': [reader, executor],
         'reclaims': reclaim_receipts})


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    require(os.environ.get('AAP_WORKTREE_MODE') == 'e2e', 'e2e worktree mode required')
    require(not any(os.environ.get(k) for k in ('AAP_PRIVATE_FIXTURE_SUPERVISED_SUITE','AAP_PRIVATE_FIXTURE_AUTHORITY_NATIVE','AAP_PRIVATE_FIXTURE_RASTER_OWNER'))
            and os.environ.get('PLAYWRIGHT_RASTER_OFFLINE') != '1', 'unexpected fixture topology')
    root = Path(os.environ['AAP_WORKTREE_RESOURCE_ROOT']).resolve(strict=True)
    require(root == ROOT.resolve() and Path.cwd().resolve() == root, 'run from the exact resource checkout root')
    owner_url, resources, identity = await _verified_database()
    require(resources['root'] == str(root) and resources['mode'] == 'e2e', 'resource identity mismatch')
    evidence = args.evidence.absolute()
    require(evidence.parent.resolve(strict=True) == evidence.parent, 'evidence parent must not contain symlinks')
    require(not os.path.lexists(evidence), 'evidence directory must be fresh')
    evidence.mkdir(mode=0o700); private_dir(evidence)
    temp = WEB / ('.t2-proof-' + uuid.uuid4().hex); temp.mkdir(mode=0o700); private_dir(temp)
    for name, content in {'config.ts': CONFIG, 'teardown.ts': TEARDOWN, 'launcher.mjs': LAUNCHER,
                          't2.spec.ts': SPEC, 'catalog.mjs': CATALOG, 'browser.mjs': BROWSER, 'closed.mjs': CLOSED}.items():
        fd = os.open(temp / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f: f.write(content); f.flush(); os.fsync(f.fileno())
    store, reader = load_store(root, resources, identity)
    engine = create_async_engine(owner_url, poolclass=NullPool, hide_parameters=True)
    b, failure_run, cancel_run = (uuid.uuid4() for _ in range(3))
    b_port, a_port, a_web, b_web = free_ports(4)
    env = dict(os.environ)
    for key in tuple(env):
        if key.startswith('SCREENSHOT_RECORDING_') or key in ('SCREENSHOT_BACKEND_REQUIREMENTS', 'SCREENSHOT_STATIC_PROFILE', 'SCREENSHOT_ASSET_OFFLINE', 'SCREENSHOT_FIXTURE_RUN_ID', 'SCREENSHOT_ML_BACKEND_MODE', 'SCREENSHOT_ML_BACKEND_URL'):
            env.pop(key)
    env['T2_EVIDENCE'] = str(evidence)
    save(evidence / 'run.json', {'runs': [str(b), str(failure_run), str(cancel_run)], 'ports': [b_port,a_port,a_web,b_web], 'temporary_sources': str(temp), 'root': str(root)})
    require_free(b_port)
    b_env = {**env, 'SCREENSHOT_FIXTURE_RUN_ID': str(b), 'PLAYWRIGHT_ISOLATED_API_PORT': str(b_port)}
    sibling = Child(['node', 'apps/web/scripts/run-screenshot-native.mjs', '--seed', '--', '--offline', '--ml-backend-mode', 'live', '--backend-requirements', 'none'], cwd=root, env=b_env, log=evidence/'b-launcher.log')
    save(evidence/'b-launcher-process.json', {'pid': sibling.pin.pid, 'start': sibling.pin.start})
    wait_for(lambda: sibling.has(f'Owned screenshot API: http://127.0.0.1:{b_port}; run: {b}') and health(b_port), [sibling.process], 'B startup')
    b_initial, b_supervisor, b_journal, b_owner = native_identity(root, resources, b, b_port)
    b_pins = pin_native(b_supervisor, b_journal); b_birth = birth_receipt(b_owner, b)
    listener_owned(b_port, b_pins['api'])
    require_free(b_web)
    vite_env = {k: env[k] for k in ('PATH','HOME','LANG') if k in env}
    vite_env.update(API_PROXY_TARGET=f'http://127.0.0.1:{b_port}', VITE_WS_HOST=f'127.0.0.1:{b_port}', PORT=str(b_web))
    vite = Child(['node', str(WEB/'node_modules/vite/bin/vite.js'), '--host', '127.0.0.1', '--strictPort'], cwd=WEB, env=vite_env, log=evidence/'b-vite.log')
    save(evidence/'b-vite-process.json', {'pid': vite.pin.pid, 'start': vite.pin.start})
    def web_ready():
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{b_web}', timeout=2) as response: return response.status == 200
        except OSError: return False
    wait_for(web_ready, [sibling.process, vite.process], 'B web startup')
    listener_owned(b_web, vite.pin)
    browser = Child(['node', str(temp/'browser.mjs'), str(b), f'http://127.0.0.1:{b_web}'], cwd=WEB, env=b_env, log=evidence/'b-browser.log')
    save(evidence/'b-browser-process.json', {'pid': browser.pin.pid, 'start': browser.pin.start})
    wait_for(lambda: browser.has('T2_B_READY'), [sibling.process, vite.process, browser.process], 'B browser readiness')
    playwright = (WEB / 'node_modules/.bin/playwright').resolve(strict=True)
    for index, (run, mode) in enumerate(((failure_run, 'failure'), (cancel_run, 'cancel')), 1):
        b_catalog = catalog_for(temp, evidence, b_env, b, f'b-before-{mode}')
        before = await snapshot(engine, root, resources, b, b_port, store, reader, b_catalog)
        require(before['native'] == b_initial, 'B native identity changed before A')
        require_free(a_port); require_free(a_web)
        a_env = {**env, 'SCREENSHOT_FIXTURE_RUN_ID': str(run), 'PLAYWRIGHT_ISOLATED_API_PORT': str(a_port),
                 'PLAYWRIGHT_ISOLATED_WEB_PORT': str(a_web), 'SCREENSHOT_STATIC_PROFILE': 'non-ml', 'SCREENSHOT_ASSET_OFFLINE': '1'}
        a = Child(['node', str(playwright), 'test', '--config=' + str(temp/'config.ts')], cwd=WEB, env=a_env, log=evidence/(mode+'-playwright.log'))
        save(evidence/(mode+'-playwright-process.json'), {'pid': a.pin.pid, 'start': a.pin.start})
        require(any(str(playwright).encode() == item for item in cmdline(a.process.pid)), 'Playwright process identity changed')
        ready = evidence / (str(run)+'.ready')
        wait_for(ready.exists, [sibling.process, vite.process, browser.process, a.process], mode + ' consumer readiness')
        marker = record(ready); require(marker['run'] == str(run) and marker['width'] == 1440 and marker['height'] == 900, 'consumer readiness identity changed')
        worker = Pin(marker['pid']); require(worker.pid in descendants(a.process.pid), 'consumer worker is not Playwright descendant')
        chromium = find_child(worker.pid, b'chrome', browser=True)
        launcher = find_child(a.process.pid, b'run-screenshot-native.mjs')
        a_initial, a_supervisor, a_journal, a_owner = native_identity(root, resources, run, a_port)
        a_pins = pin_native(a_supervisor, a_journal); a_birth = birth_receipt(a_owner, run)
        listener_owned(a_port, a_pins['api'])
        a_vite = find_listener(a.process.pid, a_web)
        save(evidence/(mode+'-children.json'), {'worker': {'pid': worker.pid, 'start': worker.start},
             'browser': {'pid': chromium.pid, 'start': chromium.start},
             'launcher': {'pid': launcher.pid, 'start': launcher.start},
             'web': {'pid': a_vite.pid, 'start': a_vite.start}})
        a_catalog = catalog_for(temp, evidence, a_env, run, 'a-'+mode)
        require({item['id'] for item in a_catalog['users'].values()}.isdisjoint(item['id'] for item in b_catalog['users'].values()), 'A and B users overlap')
        a_principals = await db_identities(engine, run)
        require(all(isinstance(item, dict) and item.get('state') == 'active' for item in a_principals),
                'A fixture principals not active')
        with reader_environment(store, reader):
            _, a_versions = await _rows_and_versions(engine, run)
            _read_originals(a_versions)
        save(evidence/(mode+'-before.json'), {'b': before, 'a_native': a_initial,
             'a_versions': a_versions, 'a_principals': a_principals})
        if mode == 'failure': worker.send(signal.SIGUSR1)
        else: a.pin.send(signal.SIGINT)
        status = a.wait(); save(evidence/(mode+'-outcome.json'), {'returncode': status, 'signal_sent': 'SIGUSR1 worker' if mode == 'failure' else 'SIGINT playwright'})
        require(status != 0, 'intentional Playwright exit unexpectedly succeeded')
        if mode == 'failure':
            require(record(evidence/(str(run)+'.enospc')) == {'run': str(run), 'code': 'ENOSPC'},
                    'real screenshot ENOSPC not observed')
        else:
            require(not os.path.lexists(evidence/(str(run)+'.enospc')), 'cancellation reached failure capture')
        require(private_read(evidence/(str(run)+'.teardown')).decode().splitlines() == ['entry','returned'], 'global teardown did not enter and return')
        receipt = record(evidence/(str(run)+'.launcher'))
        require(receipt == {'pid': launcher.pid, 'code': 0, 'signal': None}, 'native launcher did not settle normally')
        launcher.exited(); worker.exited(); chromium.exited()
        await verify_retired(engine, root, resources, run, a_initial, a_pins, a_versions, a_birth,
                             a_principals, temp, evidence, a_env, mode)
        require(sibling.pin.alive() and vite.pin.alive() and browser.pin.alive(), 'B consumer or launcher exited')
        save(evidence/f'b-command-{index}.json', f'reload-{index}')
        wait_for(lambda: browser.has(f'T2_B_reload-{index}'), [sibling.process, vite.process, browser.process], 'B browser reload')
        after_catalog = catalog_for(temp, evidence, b_env, b, f'b-after-{mode}')
        after = await snapshot(engine, root, resources, b, b_port, store, reader, after_catalog)
        require(after == before, 'B snapshot changed during A or browser reload')
        save(evidence/(mode+'-after.json'), after)
    # Exact normal B shutdown only after both independent A proofs.
    b_catalog = catalog_for(temp, evidence, b_env, b, 'b-final-live')
    b_last = await snapshot(engine, root, resources, b, b_port, store, reader, b_catalog)
    save(evidence/'b-command-3.json', 'close')
    require(browser.wait() == 0 and browser.has('T2_B_CLOSED'), 'B browser did not close normally')
    vite.pin.send(signal.SIGTERM)
    vite_status = vite.wait()
    save(evidence/'b-vite-outcome.json', {'returncode': vite_status})
    require(vite_status in (0, -signal.SIGTERM), 'B web server did not settle normally')
    vite.pin.exited()
    sibling.pin.send(signal.SIGINT); require(sibling.wait() == 0, 'B launcher did not settle normally')
    sibling.pin.exited()
    await verify_retired(engine, root, resources, b, b_initial, b_pins, b_last['versions'], b_birth,
                         b_last['principals'], temp, evidence, b_env, 'b-final')
    authority = root/'.worktree/e2e/fixture-authority'
    require(not os.path.lexists(authority/'owner.json'), 'shared authority owner remains')
    generation = record(authority/'generation.json')
    require(generation == {'resources': resources, 'nonce': b_initial['authority']['nonce'], 'state': 'closed'}, 'shared authority did not close')
    for run in (b, failure_run, cancel_run):
        require(record(authority/('suite-'+run.hex)/'generation.json') == generation, 'root closure missing')
    await engine.dispose()
    save(evidence/'complete.json', {'status': 'passed', 'limitations': ['real capture output failure, not production screenshots.spec.ts scene failure handler', 'active browser consumer, not an in-flight original read']})
    shutil.rmtree(temp)
    print('Screenshot failure/cancellation proof completed; private evidence retained.')


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        # Only our fixed proof messages are safe; driver errors may contain DSNs.
        detail = f': {error}' if isinstance(error, ProofError) else ''
        print(f'Screenshot proof failed ({type(error).__name__}){detail}; resources and evidence retained.', file=sys.stderr)
        raise SystemExit(1) from None
