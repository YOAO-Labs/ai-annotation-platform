#!/usr/bin/env python3
"""Real outbound HTTPS Agent and dataset owners in a disposable test topology.

The center and source use separate Docker bridge networks. A reachable canary
proves that the center cannot initiate site connections. Only the Agent mounts
the controlled source. Existing runtime images are reused, never rebuilt.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import secrets
import select
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from urllib import error, parse, request
import uuid

ROOT = Path(__file__).resolve().parents[1]
LABEL = "aap.e06."
MAX_RESPONSE = 4 * 1024 * 1024


def require(condition, code):
    if not condition:
        raise ValueError(code)


def private_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "w") as output:
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def run(arguments, *, timeout=45, check=True):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout)
    require(
        len(result.stdout) <= MAX_RESPONSE and len(result.stderr) <= MAX_RESPONSE,
        "command_output_budget",
    )
    if check and result.returncode:
        raise ValueError("command_failed:" + Path(arguments[0]).name)
    return result


class Owner:
    def __init__(self, state, *, recover=False):
        self.state = Path(state).resolve()
        if not recover:
            self.state.mkdir(mode=0o700)
            self.value = {
                "schema": "aap.site-agent-verification/v1",
                "root": str(ROOT),
                "nonce": secrets.token_hex(16),
                "resources": [],
            }
            self.save()
        else:
            self.value = json.loads((self.state / "owner.json").read_bytes())
        require(
            self.state.stat().st_uid == os.getuid()
            and not self.state.stat().st_mode & 0o077,
            "private_state_required",
        )
        require(
            self.value["root"] == str(ROOT)
            and self.value["schema"] == "aap.site-agent-verification/v1",
            "owner_changed",
        )
        require(re.fullmatch(r"[a-f0-9]{32}", self.value["nonce"]), "owner_nonce")

    def save(self):
        private_json(self.state / "owner.json", self.value)

    def create(self, kind, role, *, image=None, options=(), command=()):
        item = {
            "kind": kind,
            "name": f"aap-e06-{self.value['nonce'][:12]}-{role}",
            "id": None,
            "image": image,
        }
        self.value["resources"].append(item)
        self.save()
        labels = [
            "--label",
            LABEL + "owner=" + self.value["nonce"],
            "--label",
            LABEL + "checkout=" + str(ROOT),
        ]
        if kind == "container":
            args = [
                "docker",
                "create",
                "--name",
                item["name"],
                *labels,
                "--pull=never",
                "--cpus=2",
                "--memory=2g",
                "--memory-swap=2g",
                "--log-opt=max-size=2m",
                "--log-opt=max-file=1",
                *options,
                image,
                *command,
            ]
        else:
            require(kind == "network", "resource_kind")
            args = ["docker", "network", "create", *labels, *options, item["name"]]
        item["id"] = run(args).stdout.decode().strip()
        self.save()
        self.inspect(item)
        return item

    def inspect(self, item, *, absent=False):
        args = (
            ["docker", "inspect", item["id"] or item["name"]]
            if item["kind"] == "container"
            else ["docker", "network", "inspect", item["id"] or item["name"]]
        )
        result = run(args, check=False)
        if result.returncode:
            require(
                absent
                and (
                    b"no such" in result.stderr.lower()
                    or b"not found" in result.stderr.lower()
                ),
                "resource_missing",
            )
            return None
        value = json.loads(result.stdout)[0]
        labels = (
            value["Config"]["Labels"]
            if item["kind"] == "container"
            else value["Labels"]
        )
        require(
            labels.get(LABEL + "owner") == self.value["nonce"]
            and labels.get(LABEL + "checkout") == str(ROOT),
            "resource_owner_changed",
        )
        if item["image"]:
            require(value["Image"] == item["image"], "runtime_image_changed")
        return value

    def action(self, item, *arguments, check=True, timeout=45):
        self.inspect(item)
        return run(["docker", *arguments, item["id"]], check=check, timeout=timeout)

    def execute(self, item, arguments, *, timeout=45, check=True):
        self.inspect(item)
        result = run(
            ["docker", "exec", item["id"], *arguments], timeout=timeout, check=False
        )
        if check and result.returncode:
            private_json(
                self.state / "exec-failure.json",
                {
                    "resource": item["name"],
                    "stdout": result.stdout.decode(errors="replace"),
                    "stderr": result.stderr.decode(errors="replace"),
                },
            )
            raise ValueError("container_command_failed:" + item["name"])
        return result

    def cleanup(self):
        for item in sorted(
            self.value["resources"], key=lambda row: row["kind"] == "network"
        ):
            observed = self.inspect(item, absent=True)
            if observed is None:
                continue
            identifier = observed["Id"]
            if item["kind"] == "container":
                run(["docker", "rm", "-f", identifier])
            else:
                require(not observed.get("Containers"), "network_still_used")
                run(["docker", "network", "rm", identifier])
            require(self.inspect(item, absent=True) is None, "resource_not_removed")
        self.value["cleaned"] = True
        self.save()


class Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def redis_relay(bind, target):
    class Forward(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                self.forward()
            except (ConnectionResetError, BrokenPipeError):
                # Fixture containers can close a connection during teardown.
                return

        def forward(self):
            with socket.create_connection(target, timeout=3) as upstream:
                sockets = [self.request, upstream]
                while True:
                    ready, _, _ = select.select(sockets, [], [], 30)
                    if not ready:
                        continue
                    for current in ready:
                        chunk = current.recv(65536)
                        if not chunk:
                            return
                        (upstream if current is self.request else self.request).sendall(
                            chunk
                        )

    server = Relay((bind, 0), Forward)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def free_port():
    with socket.socket() as stream:
        stream.bind(("127.0.0.1", 0))
        return stream.getsockname()[1]


def replaced_host(value, host, port=None):
    url = parse.urlsplit(value)
    credentials = url.netloc.rsplit("@", 1)[0] + "@" if "@" in url.netloc else ""
    return parse.urlunsplit(
        (
            url.scheme,
            credentials
            + host
            + (":" + str(port or url.port) if port or url.port else ""),
            url.path,
            url.query,
            url.fragment,
        )
    )


def certificate(directory, addresses):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Owned E06 center")])
    stamp = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(stamp - timedelta(minutes=1))
        .not_valid_after(stamp + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(address)) for address in addresses]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (directory / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    os.chmod(directory / "key.pem", 0o600)


class HTTP:
    def __init__(self, origin, ca):
        self.origin, self.token = origin, None
        self.opener = request.build_opener(
            request.ProxyHandler({}),
            request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca))),
        )

    def call(
        self, method, path, data=None, *, expected=200, binary=False, headers=None
    ):
        fields = {
            "Content-Type": "application/json",
            **({"Authorization": "Bearer " + self.token} if self.token else {}),
            **(headers or {}),
        }
        payload = json.dumps(data).encode() if data is not None else None
        try:
            response = self.opener.open(
                request.Request(
                    self.origin + path, data=payload, method=method, headers=fields
                ),
                timeout=90,
            )
        except error.HTTPError as exc:
            response = exc
        with response:
            body = response.read(MAX_RESPONSE + 1)
            require(len(body) <= MAX_RESPONSE, "http_output_budget")
            allowed = {expected} if type(expected) is int else set(expected)
            if response.status not in allowed:
                code = "unexpected_http_status:" + str(response.status)
                try:
                    detail = json.loads(body).get("detail")
                    if isinstance(detail, dict) and re.fullmatch(
                        r"[a-z0-9_]{1,100}", str(detail.get("code", ""))
                    ):
                        code += ":" + detail["code"]
                except (ValueError, AttributeError):
                    pass
                raise ValueError(code)
            if binary:
                return body, response.headers
            return json.loads(body) if body else None


async def seed(database, output):
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.config import settings
    from app.core.security import create_access_token, hash_password
    from app.db.models.user import User

    require(
        os.environ.get("AAP_WORKTREE_MODE") == "test" and database.endswith("_test"),
        "test_mode_required",
    )
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            require(
                await db.scalar(text("SELECT current_database()")) == database,
                "seed_database_changed",
            )
            require(
                await db.scalar(select(func.count()).select_from(User)) == 0,
                "test_database_not_empty",
            )
            actor = User(
                id=uuid.uuid4(),
                email="e06-" + uuid.uuid4().hex + "@fixture.invalid",
                name="E06 fixture administrator",
                password_hash=hash_password(secrets.token_urlsafe(24)),
                role="super_admin",
                is_active=True,
            )
            db.add(actor)
            await db.commit()
            private_json(
                output,
                {
                    "actor_id": str(actor.id),
                    "token": create_access_token(
                        subject=str(actor.id), role="super_admin"
                    ),
                },
            )
    finally:
        await engine.dispose()


def wait_until(call, predicate, *, seconds=90):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = call()
        if predicate(last):
            return last
        time.sleep(0.2)
    raise ValueError("observation_timeout")


def verify(owner, image, *, scenario="basic", ui_hold_seconds=0):
    from PIL import Image
    from worktree_env import load_identity, topology

    require(
        os.environ.get("AAP_WORKTREE_MODE") == "test", "launcher_test_mode_required"
    )
    resources = topology(ROOT, load_identity(ROOT), "test")
    require(
        all(
            os.environ.get(key) == value for key, value in resources["buckets"].items()
        ),
        "test_buckets_changed",
    )
    require(
        parse.urlsplit(os.environ["DATABASE_URL"]).path.removeprefix("/")
        == resources["database"],
        "test_database_changed",
    )
    require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", image), "immutable_cached_image_required"
    )
    require(
        json.loads(run(["docker", "image", "inspect", image]).stdout)[0]["Id"] == image,
        "cached_image_required",
    )
    require(
        scenario in {"basic", "recovery", "replicas"}, "unknown_site_agent_scenario"
    )
    require(
        type(ui_hold_seconds) is int and 0 <= ui_hold_seconds <= 300,
        "ui_hold_out_of_bounds",
    )
    require(not ui_hold_seconds or scenario == "replicas", "ui_hold_requires_replicas")
    summary = {
        "scope": "two isolated Linux sites"
        if scenario == "replicas"
        else "one isolated Linux site",
        "scenario": scenario,
        "steps": [],
        "e06_complete": False,
    }

    def record(name, value=True):
        summary["steps"].append({"name": name, "value": value})
        private_json(owner.state / "summary.json", summary)
        print(name, flush=True)

    center_net = owner.create("network", "center-net")
    site_net = owner.create("network", "site-net")
    replica_net = (
        owner.create("network", "replica-net") if scenario == "replicas" else None
    )
    center_gateway = owner.inspect(center_net)["IPAM"]["Config"][0]["Gateway"]
    site_gateway = owner.inspect(site_net)["IPAM"]["Config"][0]["Gateway"]
    replica_gateway = (
        owner.inspect(replica_net)["IPAM"]["Config"][0]["Gateway"]
        if replica_net
        else None
    )
    center_dir, site_dir, source = [
        owner.state / name for name in ("center", "site", "source")
    ]
    for directory in (center_dir, site_dir, source):
        directory.mkdir(mode=0o700)
    for child in ("mounted/dataset/child",):
        (source / child).mkdir(parents=True)
    files = {}
    for name, size, number in [("a.png", (512, 512), 1), ("child/b.png", (64, 48), 2)]:
        picture = Image.frombytes(
            "RGB", size, random.Random(number).randbytes(size[0] * size[1] * 3)
        )
        output = io.BytesIO()
        picture.save(output, format="PNG")
        files[name] = output.getvalue()
        (source / "mounted/dataset" / name).write_bytes(files[name])
    certificate(
        center_dir,
        ["127.0.0.1", site_gateway, *([replica_gateway] if replica_gateway else [])],
    )
    shutil.copyfile(center_dir / "ca.pem", site_dir / "ca.pem")
    redis_url = parse.urlsplit(os.environ["REDIS_URL"])
    relay = redis_relay(center_gateway, (redis_url.hostname, redis_url.port))
    try:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("MINIO_", "CELERY_"))
            or key in {"DATABASE_URL", "AAP_WORKTREE_MODE"}
        }
        for key in [
            "DATABASE_URL",
            "REDIS_URL",
            "CELERY_BROKER_URL",
            "CELERY_BROKER_READ_URL",
            "CELERY_BROKER_WRITE_URL",
            "CELERY_RESULT_BACKEND",
        ]:
            value = os.environ[key]
            environment[key] = replaced_host(
                value,
                center_gateway,
                relay.server_address[1] if key != "DATABASE_URL" else None,
            )
        environment.update(
            ENVIRONMENT="development",
            SECRET_KEY=secrets.token_urlsafe(48),
            DEBUG="false",
            LOG_LEVEL="WARNING",
            ML_MANAGED_BACKEND_URLS="{}",
            ML_MANAGED_AUTH_DIR="/runtime/inference-auth",
            ML_BACKEND_OPERATIONS_DIR="/runtime/operations",
            DUCKDB_PATH="/runtime/analytics.duckdb",
            PYTHONDONTWRITEBYTECODE="1",
            STORAGE_MAINTENANCE_PROTOCOL_ENABLED="true",
            PYTHONPATH="/app:/_shared/filesystem_read/src:/_shared/site_agent_contract/src:/_shared/task_contract/src:/_shared/training_contract:/_shared/protocol_v2/src",
        )
        endpoint = parse.urlsplit("http://" + environment["MINIO_ENDPOINT"])
        environment["MINIO_ENDPOINT"] = center_gateway + ":" + str(endpoint.port)
        envfile = center_dir / "runtime.env"
        envfile.write_text(
            "".join(key + "=" + value + "\n" for key, value in environment.items())
        )
        os.chmod(envfile, 0o600)
        port = free_port()
        common = [
            "--network",
            center_net["id"],
            "--user",
            str(os.getuid()),
            "--entrypoint",
            "python",
            "--env-file",
            str(envfile),
            "--volume",
            str(ROOT / "apps/api") + ":/app:ro",
            "--volume",
            str(ROOT / "apps/_shared") + ":/_shared:ro",
            "--volume",
            str(center_dir) + ":/runtime",
            "--volume",
            str(Path(__file__).resolve()) + ":/fixture/verify_site_agent.py:ro",
        ]
        center = owner.create(
            "container",
            "api",
            image=image,
            options=[
                *common,
                "--publish",
                f"127.0.0.1:{port}:8443",
                "--publish",
                f"{site_gateway}:{port}:8443",
                *(
                    ["--publish", f"{replica_gateway}:{port}:8443"]
                    if replica_gateway
                    else []
                ),
            ],
            command=[
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8443",
                "--ssl-certfile",
                "/runtime/ca.pem",
                "--ssl-keyfile",
                "/runtime/key.pem",
                "--no-access-log",
            ],
        )
        owner.action(center, "start")
        http = HTTP(f"https://127.0.0.1:{port}", center_dir / "ca.pem")
        deadline = time.monotonic() + 60
        while True:
            require(owner.inspect(center)["State"]["Running"], "center_exited")
            try:
                if http.call("GET", "/health/db")["status"] == "ok":
                    break
            except (OSError, ValueError):
                require(time.monotonic() < deadline, "center_not_ready")
                time.sleep(0.2)
        owner.execute(
            center,
            [
                "python",
                "/fixture/verify_site_agent.py",
                "--seed",
                resources["database"],
                "--seed-output",
                "/runtime/identity.json",
            ],
        )
        http.token = json.loads((center_dir / "identity.json").read_bytes())["token"]
        record("real_center_https_ready")
        node = "e06-" + owner.value["nonce"][:12] + "@central"
        worker = owner.create(
            "container",
            "worker",
            image=image,
            options=common,
            command=[
                "-m",
                "celery",
                "-A",
                "app.workers.celery_app",
                "worker",
                "--pool=solo",
                "--concurrency=1",
                "--hostname=" + node,
                "--queues=default",
                "--loglevel=WARNING",
            ],
        )
        owner.action(worker, "start")
        code = (
            "from app.workers.celery_app import celery_app; r=celery_app.control.inspect(destination=["
            + repr(node)
            + "],timeout=2).registered(); print('ready' if r and 'app.workers.dataset_scan.run_dataset_scan' in r.get("
            + repr(node)
            + ",[]) else 'waiting')"
        )
        wait_until(
            lambda: owner.execute(center, ["python", "-c", code]).stdout.strip(),
            lambda value: value == b"ready",
            seconds=45,
        )
        canary = owner.create(
            "container",
            "canary",
            image=image,
            options=["--network", site_net["id"], "--entrypoint", "python"],
            command=[
                "-c",
                "import socket; s=socket.socket(); s.bind(('0.0.0.0',8123)); s.listen();\nwhile True:\n c,_=s.accept(); c.close()",
            ],
        )
        owner.action(canary, "start")
        canary_ip = owner.inspect(canary)["NetworkSettings"]["Networks"][
            site_net["name"]
        ]["IPAddress"]
        probe = "import socket; s=socket.create_connection(('127.0.0.1',8123),timeout=2); s.close()"
        owner.execute(canary, ["python", "-c", probe])
        probe = (
            "import socket\ntry:\n s=socket.create_connection(("
            + repr(canary_ip)
            + ",8123),timeout=2)\nexcept OSError:\n print('isolated')\nelse:\n s.close(); raise SystemExit(3)"
        )
        require(
            owner.execute(center, ["python", "-c", probe]).stdout.strip()
            == b"isolated",
            "center_can_enter_site",
        )
        record("center_inbound_site_connection_blocked")
        site = http.call(
            "POST",
            "/api/v1/site-agents/sites",
            {"name": "Owned remote site " + owner.value["nonce"][:8]},
            expected=201,
        )
        pairing = http.call(
            "POST",
            "/api/v1/site-agents/sites/" + site["id"] + "/agents",
            {
                "name": "Owned Linux Agent",
                **(
                    {"limits": {"bandwidth_bytes_per_second": 64 * 1024}}
                    if scenario == "recovery"
                    else {}
                ),
            },
            expected=201,
        )
        private_json(
            site_dir / "pair.json",
            {"agent_id": pairing["agent_id"], "pair_code": pairing["pair_code"]},
        )
        private_json(
            site_dir / "config.json",
            {
                "center_url": f"https://{site_gateway}:{port}",
                "state_dir": "/agent-private/state",
                "credential_file": "/agent-private/identity.json",
                "roots": {"images": {"path": "/source", "revision": 1}},
                "ca_file": "/agent-private/ca.pem",
            },
        )
        bootstrap = "import os,pathlib,sys,time\nwhile not pathlib.Path('/agent-private/identity.json').exists(): time.sleep(.1)\nos.execv(sys.executable,[sys.executable,'-m','storage_agent','--config','/agent-private/config.json'])\n"
        (site_dir / "run.py").write_text(bootstrap)
        agent = owner.create(
            "container",
            "agent",
            image=image,
            options=[
                "--network",
                site_net["id"],
                "--user",
                str(os.getuid()),
                "--entrypoint",
                "python",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--env",
                "PYTHONPATH=/code/agent:/code/fs:/code/wire",
                "--volume",
                str(ROOT / "apps/storage-agent") + ":/code/agent:ro",
                "--volume",
                str(ROOT / "apps/_shared/filesystem_read/src") + ":/code/fs:ro",
                "--volume",
                str(ROOT / "apps/_shared/site_agent_contract/src") + ":/code/wire:ro",
                "--volume",
                str(source) + ":/source:ro",
                "--volume",
                str(site_dir) + ":/agent-private",
            ],
            command=["/agent-private/run.py"],
        )
        owner.action(agent, "start")
        require(
            owner.execute(
                agent,
                [
                    "python",
                    "-m",
                    "storage_agent",
                    "--config",
                    "/agent-private/config.json",
                    "--pairing-file",
                    "/agent-private/pair.json",
                ],
            ).stdout.strip()
            == b"agent_paired",
            "pairing_failed",
        )
        wait_until(
            lambda: http.call(
                "GET", "/api/v1/site-agents/agents?agent_id=" + pairing["agent_id"]
            ),
            lambda value: value["items"] and value["items"][0]["state"] == "online",
        )
        record("standalone_agent_paired_and_heartbeat_received")
        location = http.call(
            "POST",
            "/api/v1/storage-locations/agent",
            {
                "name": "Owned remote images",
                "agent_id": pairing["agent_id"],
                "root_handle": "images",
                "root_revision": 1,
                "relative_path": "mounted",
                "scope": "global",
                "cache_policy": "none",
            },
            expected=201,
        )
        probe = http.call(
            "POST",
            "/api/v1/storage-locations/" + location["id"] + "/probe",
            {"sample_file": "dataset/a.png"},
        )
        require(probe["ok"], "remote_probe_failed")
        dataset = http.call(
            "POST", "/api/v1/datasets", {"name": "Owned remote dataset"}, expected=201
        )
        source_row = http.call(
            "POST",
            "/api/v1/dataset-sources",
            {
                "name": "Outbound source",
                "dataset_id": dataset["id"],
                "location_id": location["id"],
                "relative_path": "dataset",
                "node_id": node,
                "cache_policy": "none",
                **({"include_globs": ["*.png"]} if scenario == "recovery" else {}),
            },
            expected=201,
        )
        scan = http.call(
            "POST",
            "/api/v1/dataset-sources/" + source_row["id"] + "/scans",
            expected=202,
        )
        scan = wait_until(
            lambda: http.call("GET", "/api/v1/dataset-scans/" + scan["id"]),
            lambda value: value["status"] in {"completed", "failed", "cancelled"},
        )
        require(
            scan["complete"] and scan["status"] == "completed", "remote_scan_failed"
        )
        record("independent_worker_completed_remote_inventory", scan["counts"])
        registration = http.call(
            "POST",
            "/api/v1/dataset-scans/" + scan["id"] + "/registration",
            expected=202,
        )

        def get_registration():
            return http.call(
                "GET", "/api/v1/dataset-scans/" + scan["id"] + "/registration"
            )

        registration = wait_until(
            get_registration, lambda value: value["status"] in {"completed", "failed"}
        )
        require(
            registration["status"] == "completed"
            and registration["counts"]["ready_count"] == 2,
            "remote_preflight_failed",
        )
        http.call(
            "POST",
            "/api/v1/dataset-registrations/" + registration["id"] + "/confirm",
            expected=202,
        )
        registration = wait_until(
            get_registration,
            lambda value: (
                value["phase"] == "registering"
                and value["status"] in {"completed", "failed"}
            ),
        )
        require(
            registration["status"] == "completed"
            and registration["counts"]["registered_count"] == 2
            and registration["counts"]["original_copy_bytes"] == 0,
            "remote_registration_failed",
        )
        record("explicit_registration_without_original_copy", registration["counts"])
        if scenario == "replicas":
            from site_agent_replicas import Replicas

            Replicas(
                owner=owner,
                http=http,
                image=image,
                center=center,
                worker=worker,
                agent=agent,
                network=site_net,
                replica_network=replica_net,
                replica_gateway=replica_gateway,
                port=port,
                site_dir=site_dir,
                source_root=source,
                dataset=dataset,
                source=source_row,
                location=location,
                files=files,
                node=node,
                record=record,
            ).run(ui_hold_seconds=ui_hold_seconds)
            summary["passed"] = True
            private_json(owner.state / "summary.json", summary)
            return summary
        if scenario == "recovery":
            from site_agent_recovery import Recovery

            Recovery(
                owner=owner,
                http=http,
                agent=agent,
                network=site_net,
                site_dir=site_dir,
                source_root=source,
                source=source_row,
                dataset=dataset,
                location=location,
                files=files,
                record=record,
            ).run()
            summary["passed"] = True
            private_json(owner.state / "summary.json", summary)
            return summary
        entries = http.call("GET", "/api/v1/dataset-scans/" + scan["id"] + "/entries")[
            "items"
        ]

        def verified_read(path, expected):
            sha = hashlib.sha256(expected).hexdigest()
            received, headers = http.call("GET", path, binary=True)
            require(
                received == expected
                and headers["X-Content-SHA256"] == sha
                and headers["Accept-Ranges"] == "bytes",
                "remote_content_changed",
            )
            selected, headers = http.call(
                "GET",
                path,
                expected=206,
                binary=True,
                headers={"Range": "bytes=17-271", "If-Range": f'"sha256:{sha}"'},
            )
            require(
                selected == expected[17:272]
                and headers["Content-Range"] == f"bytes 17-271/{len(expected)}"
                and headers["X-Content-SHA256"] == sha,
                "verified_range_changed",
            )
            suffix, _ = http.call(
                "GET", path, expected=206, binary=True, headers={"Range": "bytes=-32"}
            )
            require(suffix == expected[-32:], "verified_suffix_changed")
            full, _ = http.call(
                "GET",
                path,
                binary=True,
                headers={"Range": "bytes=0-31", "If-Range": '"obsolete"'},
            )
            require(full == expected, "obsolete_if_range_did_not_return_full")
            http.call(
                "GET",
                path,
                expected=416,
                binary=True,
                headers={"Range": f"bytes={len(expected)}-"},
            )

        for entry in entries:
            if entry["kind"] != "file":
                continue
            expected = files[entry["relative_path"]]
            sha = hashlib.sha256(expected).hexdigest()
            path = (
                "/api/v1/dataset-scans/"
                + scan["id"]
                + "/entries/"
                + entry["path_digest"]
                + "/content?sha256="
                + sha
            )
            verified_read(path, expected)
        record("real_https_scan_originals_and_ranges_match_full_sha")
        items = http.call("GET", "/api/v1/datasets/" + dataset["id"] + "/items")[
            "items"
        ]
        require(len(items) == 2, "registered_items_missing")
        for item in items:
            expected = next(
                data
                for name, data in files.items()
                if name.rsplit("/", 1)[-1] == item["file_name"]
            )
            verified_read(
                "/api/v1/dataset-items/"
                + item["id"]
                + "/content?sha256="
                + hashlib.sha256(expected).hexdigest(),
                expected,
            )
        record("real_https_registered_originals_and_ranges_match_full_sha")
        require(
            all(
                (source / "mounted/dataset" / name).read_bytes() == data
                for name, data in files.items()
            ),
            "source_bytes_changed",
        )
        summary["passed"] = True
        private_json(owner.state / "summary.json", summary)
        return summary
    finally:
        relay.shutdown()
        relay.server_close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--runtime-image")
    parser.add_argument(
        "--scenario", choices=("basic", "recovery", "replicas"), default="basic"
    )
    parser.add_argument("--ui-hold-seconds", type=int, default=0)
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--seed")
    parser.add_argument("--seed-output", type=Path)
    args = parser.parse_args()
    if args.seed:
        asyncio.run(seed(args.seed, args.seed_output))
        return 0
    require(
        args.state is not None and args.state.is_absolute(), "absolute_state_required"
    )
    owner = Owner(args.state, recover=args.cleanup)
    if args.cleanup:
        owner.cleanup()
        return 0
    try:
        verify(
            owner,
            args.runtime_image,
            scenario=args.scenario,
            ui_hold_seconds=args.ui_hold_seconds,
        )
    except BaseException as exc:
        for item in owner.value["resources"]:
            if (
                item["kind"] == "container"
                and owner.inspect(item, absent=True) is not None
            ):
                result = owner.action(item, "logs", check=False)
                private_json(
                    owner.state / (item["name"] + "-log.json"),
                    {
                        "stdout": result.stdout.decode(errors="replace"),
                        "stderr": result.stderr.decode(errors="replace"),
                    },
                )
        private_json(
            owner.state / "failure.json",
            {
                "type": type(exc).__name__,
                "code": str(exc)
                if isinstance(exc, ValueError)
                else "verification_failed",
            },
        )
        raise
    finally:
        owner.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
