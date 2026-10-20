"""Owned loopback SMTP and installed-process helpers for recovery acceptance."""

from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
import json
import os
import socketserver
import subprocess
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.engine import make_url

from tests.test_identity_admission_live import (
    INSTALLATION_PROBE,
    ROOT,
    Container,
    _cleanup,
    _dsn,
    _port,
    _start,
    _verify_installation,
)
from tests.test_asset_storage_cleanup_runtime import _docker, _inspect_docker


def command(operation, payload, *, operation_id=None):
    return {
        "schema": "aap-identity-recovery/v1",
        "operation": operation,
        "operation_id": str(operation_id or uuid.uuid4()),
        "audit_context": {
            "method": "POST",
            "path": "/acceptance/recovery",
            "ip": "127.0.0.1",
            "request_id": None,
        },
        "payload": payload,
    }


def node(identity, control, request):
    result = subprocess.run(
        ["node", str(ROOT / "apps/api/tests/fixtures/identity_recovery_peer.mjs")],
        input=json.dumps(
            {"url": identity.origin, "control_token": control, "request": request}
        ),
        text=True,
        capture_output=True,
        env={"PATH": os.environ.get("PATH", "")},
        timeout=45,
    )
    assert result.returncode == 0, "Independent recovery peer failed"
    return json.loads(result.stdout)


async def cleanup(maker, actors, operations):
    async with maker.begin() as db:
        assert (await db.scalar(text("SELECT current_database()"))).endswith("_test")
        await db.execute(
            text(
                "DELETE FROM identity_recovery_operation_receipts WHERE operation_id=ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list(operations)},
        )
        for table, column in (
            ("identity_credential_mail_outbox", "actor_id"),
            ("password_reset_tokens", "user_id"),
            ("email_verification_tokens", "user_id"),
        ):
            await db.execute(
                text(f"DELETE FROM {table} WHERE {column}=ANY(CAST(:ids AS uuid[]))"),
                {"ids": list(actors)},
            )
    for actor in actors:
        await _cleanup(maker, actor, [])


class SMTPInbox:
    """Minimal real SMTP peer: no external relay and no credential logging."""

    def __init__(self, *, lose_ack=0, pause_ack=False):
        self.messages = []
        self.lose_ack = lose_ack
        self.condition = threading.Condition()
        self.release = threading.Event()
        if not pause_ack:
            self.release.set()
        inbox = self

        class Handler(socketserver.StreamRequestHandler):
            def send(self, value):
                self.wfile.write(value + b"\r\n")
                self.wfile.flush()

            def handle(self):
                self.connection.settimeout(12)
                self.send(b"220 loopback.test ESMTP")
                while line := self.rfile.readline(65537):
                    verb = line.split(b" ", 1)[0].strip().upper()
                    if verb in {b"EHLO", b"HELO"}:
                        self.send(b"250 loopback.test")
                    elif verb in {b"MAIL", b"RCPT", b"RSET", b"NOOP"}:
                        self.send(b"250 OK")
                    elif verb == b"DATA":
                        self.send(b"354 End with dot")
                        body = bytearray()
                        while data := self.rfile.readline(65537):
                            if data == b".\r\n":
                                break
                            body.extend(data[1:] if data.startswith(b"..") else data)
                        message = BytesParser(policy=policy.default).parsebytes(
                            bytes(body)
                        )
                        with inbox.condition:
                            inbox.messages.append(message)
                            drop = inbox.lose_ack > 0
                            if drop:
                                inbox.lose_ack -= 1
                            inbox.condition.notify_all()
                        if not inbox.release.wait(12) or drop:
                            return
                        self.send(b"250 Accepted")
                    elif verb == b"QUIT":
                        self.send(b"221 Bye")
                        return
                    else:
                        self.send(b"502 Unsupported")

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.messages.clear()

    async def wait(self, count):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.condition:
                if len(self.messages) >= count:
                    return
            await asyncio.sleep(0.03)
        raise AssertionError("Owned loopback SMTP did not receive the expected mail")

    def tokens(self):
        result = []
        with self.condition:
            for message in self.messages:
                for line in message.get_content().splitlines():
                    result.extend(
                        parse_qs(urlsplit(line.strip()).query).get("token", [])
                    )
        return result


def start_mail(test_engine, directory, key, inbox, *, port=None):
    values = {
        "DATABASE_URL": _dsn("MAIL", test_engine),
        "SETTINGS_DATABASE_URL": _dsn("MAIL_SETTINGS", test_engine),
        "ENCRYPTION_KEY": key,
    }
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "IDENTITY_MAIL_HOST": "127.0.0.1",
        "IDENTITY_MAIL_PORT": str(port or _port()),
        "IDENTITY_MAIL_SMTP_HOST": "127.0.0.1",
        "IDENTITY_MAIL_SMTP_PORT": str(inbox.port),
        "IDENTITY_MAIL_SMTP_FROM": "noreply@example.test",
        "IDENTITY_MAIL_POLL_INTERVAL_SECONDS": "0.1",
        "IDENTITY_MAIL_LEASE_SECONDS": "2",
        "IDENTITY_MAIL_RETRY_DELAY_SECONDS": "1",
    }
    for name, value in values.items():
        path = directory / f"mail-{name.lower()}"
        path.write_text(value)
        path.chmod(0o600)
        environment[f"IDENTITY_MAIL_{name}_FILE"] = str(path)
    assert all(
        name == "PATH" or name.startswith("IDENTITY_MAIL_") for name in environment
    )
    return _start(
        [
            os.environ["AAP_IDENTITY_RUNTIME_PYTHON"],
            "-I",
            "-m",
            "aap_identity_runtime.mail_worker",
        ],
        environment,
        directory,
        directory / f"mail-{environment['IDENTITY_MAIL_PORT']}.log",
        int(environment["IDENTITY_MAIL_PORT"]),
        "/ready",
    )


async def start_mail_container(test_engine, directory, key, inbox):
    image = _inspect_docker(os.environ["AAP_IDENTITY_RUNTIME_IMAGE"])
    identity = os.environ["AAP_IDENTITY_RUNTIME_IDENTITY"]
    assert image["Config"]["Labels"] == {
        "org.aap.acceptance": "identity-admission",
        "org.aap.acceptance-id": identity,
    }
    values = {
        "IDENTITY_MAIL_DATABASE_URL": make_url(_dsn("MAIL", test_engine))
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False),
        "IDENTITY_MAIL_SETTINGS_DATABASE_URL": make_url(
            _dsn("MAIL_SETTINGS", test_engine)
        )
        .set(host="host.docker.internal")
        .render_as_string(hide_password=False),
        "IDENTITY_MAIL_ENCRYPTION_KEY": key,
        "IDENTITY_MAIL_HOST": "0.0.0.0",
        "IDENTITY_MAIL_PORT": "8171",
        "IDENTITY_MAIL_SMTP_HOST": "host.docker.internal",
        "IDENTITY_MAIL_SMTP_PORT": str(inbox.port),
        "IDENTITY_MAIL_SMTP_FROM": "noreply@example.test",
        "IDENTITY_MAIL_POLL_INTERVAL_SECONDS": "0.1",
        "IDENTITY_MAIL_LEASE_SECONDS": "2",
        "IDENTITY_MAIL_RETRY_DELAY_SECONDS": "1",
    }
    path = directory / "mail-container.env"
    path.touch(mode=0o600)
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()))
    container = None
    try:
        container_id = _docker(
            "run",
            "--detach",
            "--name",
            f"aap-identity-mail-{uuid.uuid4().hex[:12]}",
            "--label=org.aap.acceptance=module-data-boundary",
            f"--label=org.aap.acceptance-id={identity}",
            "--add-host=host.docker.internal:host-gateway",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--memory=256m",
            "--cpus=1",
            "--pids-limit=64",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            "--publish=127.0.0.1::8171",
            "--env-file",
            str(path),
            "--health-cmd",
            "python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:8171/ready', timeout=2)\"",
            image["Id"],
            "python",
            "-m",
            "aap_identity_runtime.mail_worker",
        )
        container = Container(container_id, identity, "")
        details = _inspect_docker(container_id)
        assert (
            details["Image"] == image["Id"]
            and details["HostConfig"]["ReadonlyRootfs"] is True
        )
        assert details["Config"]["User"] == "10003:10003" and details["Mounts"] == []
        names = {value.split("=", 1)[0] for value in details["Config"]["Env"]}
        assert not names.intersection(
            {
                "IDENTITY_DATABASE_URL",
                "IDENTITY_TOKEN",
                "IDENTITY_SIGNING_SECRET",
                "IDENTITY_RECOVERY_DATABASE_URL",
                "IDENTITY_RECOVERY_MAIL_KEY",
                "DATABASE_URL",
                "REDIS_URL",
                "CELERY_BROKER_URL",
            }
        )
        ports = details["NetworkSettings"]["Ports"]["8171/tcp"]
        assert len(ports) == 1 and ports[0]["HostIp"] == "127.0.0.1"
        container.origin = f"http://127.0.0.1:{ports[0]['HostPort']}"
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                assert _inspect_docker(container_id)["State"]["Running"]
                try:
                    if (
                        await client.get(container.origin + "/ready")
                    ).status_code == 200:
                        report = _verify_installation(
                            _docker(
                                "exec",
                                container_id,
                                "python",
                                "-I",
                                "-c",
                                INSTALLATION_PROBE,
                            )
                        )
                        assert report["uid"] == 10003 and report["tmpfs"]
                        return container
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        raise AssertionError("Owned mail container did not become ready")
    except BaseException:
        if container is not None:
            container.stop()
        raise
    finally:
        path.unlink(missing_ok=True)


async def outbox(maker, operation_id):
    async with maker() as db:
        return (
            await db.execute(
                text(
                    "SELECT status,attempt,last_reason,lease_expires_at,token_hash,token_ciphertext FROM identity_credential_mail_outbox WHERE id=:id"
                ),
                {"id": operation_id},
            )
        ).one()


async def wait_status(maker, operation_id, status):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        row = await outbox(maker, operation_id)
        if row.status == status:
            return row
        await asyncio.sleep(0.03)
    raise AssertionError("Owned mail intent did not reach the expected state")
