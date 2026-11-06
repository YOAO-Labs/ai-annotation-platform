"""Fixture-owner credentials cannot reach ordinary routes or another database."""

import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import textwrap
import uuid

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "web/e2e/fixtures/seed-request.py"


@pytest.mark.parametrize(
    "path,owner_database,runtime_database,host,mode",
    [
        ("/api/v1/users", "fixture_e2e", "fixture_e2e", "127.0.0.1", "e2e"),
        (
            "/api/v1/__test/seed/../../users",
            "fixture_e2e",
            "fixture_e2e",
            "127.0.0.1",
            "e2e",
        ),
        ("/api/v1/__test/seed/owned", "development", "development", "127.0.0.1", "e2e"),
        ("/api/v1/__test/seed/owned", "first_e2e", "second_e2e", "127.0.0.1", "e2e"),
        (
            "/api/v1/__test/seed/owned",
            "fixture_e2e",
            "fixture_e2e",
            "remote.invalid",
            "e2e",
        ),
        ("/api/v1/__test/seed/owned", "fixture_e2e", "fixture_e2e", "127.0.0.1", "dev"),
    ],
)
def test_owner_seed_refuses_cross_boundary_setup_without_leaking_credentials(
    path, owner_database, runtime_database, host, mode
):
    secret = "fixture-secret-not-for-output"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(
            {
                "action": "execute",
                "operation_id": str(uuid.uuid4()),
                "payload": {"path": path, "data": {"namespace": "guarded"}},
            }
        ),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ENVIRONMENT": "development",
            "AAP_WORKTREE_MODE": mode,
            "PLAYWRIGHT_E2E_DATABASE_URL": f"postgresql+asyncpg://owner:{secret}@{host}/{owner_database}",
            "DATABASE_URL": f"postgresql+asyncpg://runtime:{secret}@127.0.0.1/{runtime_database}",
        },
        timeout=10,
    )
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "runtime_database,enabled,environment",
    [
        ("foreign_e2e", "true", "development"),
        ("fixture_e2e", "false", "development"),
        ("fixture_e2e", "true", "production"),
    ],
)
def test_mask_lifecycle_owner_refuses_foreign_database_or_disabled_e2e(
    runtime_database, enabled, environment
):
    secret = "fixture-secret-not-for-output"
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=json.dumps(
            {
                "action": "execute",
                "operation_id": str(uuid.uuid4()),
                "payload": {"operation": "mask_slice_lifecycle", "data": {}},
            }
        ),
        text=True,
        capture_output=True,
        env={
            **os.environ,
            "ENVIRONMENT": environment,
            "E2E_SEED_ENABLED": enabled,
            "AAP_WORKTREE_MODE": "e2e",
            "PLAYWRIGHT_E2E_DATABASE_URL": f"postgresql+asyncpg://owner:{secret}@127.0.0.1/fixture_e2e",
            "DATABASE_URL": f"postgresql+asyncpg://runtime:{secret}@127.0.0.1/{runtime_database}",
        },
        timeout=10,
    )
    assert result.returncode == 1
    assert secret not in result.stdout + result.stderr


def test_authority_recovers_actual_http_response_when_disposal_fails(tmp_path):
    """Use the real router and authority; only resource verification is isolated."""
    root = Path(__file__).resolve().parents[3]
    directory = tmp_path / ".worktree" / "e2e" / "fixture-authority"
    directory.mkdir(parents=True, mode=0o700)
    resources = {"root": str(tmp_path), "mode": "e2e"}
    generation = {"resources": resources, "nonce": "a" * 64, "state": "starting"}
    generation_path = directory / "generation.json"
    generation_path.write_text(json.dumps(generation))
    generation_path.chmod(0o600)
    read_fd, write_fd = os.pipe()
    program = textwrap.dedent("""
        import asyncio, importlib.util, json, os, sys
        from pathlib import Path
        root, directory, ready_fd = sys.argv[1], Path(sys.argv[2]), int(sys.argv[3])
        os.environ.update({
            "ENVIRONMENT": "development", "E2E_SEED_ENABLED": "true",
            "AAP_WORKTREE_MODE": "e2e",
            "DATABASE_URL": "postgresql+asyncpg://runtime:secret@127.0.0.1/fixture_e2e",
            "PLAYWRIGHT_E2E_DATABASE_URL": "postgresql+asyncpg://owner:secret@127.0.0.1/fixture_e2e",
        })
        sys.path.insert(0, str(Path(root) / "scripts"))
        sys.path.insert(0, str(Path(root) / "apps/api"))
        spec = importlib.util.spec_from_file_location("seed_request_test", Path(root) / "apps/web/e2e/fixtures/seed-request.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        resources = {"root": str(directory.parents[2]), "mode": "e2e"}
        module.owner_paths = lambda: (resources, directory)
        import fixture_suite_coordinator
        async def verified_database():
            return os.environ["PLAYWRIGHT_E2E_DATABASE_URL"], resources, {"fixture": "isolated"}
        fixture_suite_coordinator._verified_database = verified_database
        from sqlalchemy.ext.asyncio import AsyncEngine
        async def fail_disposal(self, *args, **kwargs):
            raise OSError("controlled engine disposal failure")
        AsyncEngine.dispose = fail_disposal
        asyncio.run(module.serve_authority(ready_fd))
    """)
    runtime_tmp = tmp_path / ("worktree-temporary-" + "x" * 100)
    runtime_tmp.mkdir(mode=0o700)
    process = None
    record = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-c", program, str(root), str(directory), str(write_fd)],
            cwd=root / "apps/api",
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env={**os.environ, "TMPDIR": str(runtime_tmp)},
        )
        os.close(write_fd)
        write_fd = None
        assert select.select([read_fd], [], [], 30)[0]
        ready = os.read(read_fd, 1)
        if ready != b"1":
            process.wait(timeout=10)
            diagnostic = tmp_path / "authority-startup.stderr"
            diagnostic.write_bytes(process.stderr.read())
            pytest.fail(
                f"Authority did not become ready; private diagnostic: {diagnostic}"
            )
        record = json.loads((directory / "owner.json").read_text())
        operation_id = str(uuid.uuid4())
        payload = {"path": "/api/v1/__test/seed/unknown-owned-operation", "data": {}}
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(30)
            channel.connect(record["socket"])
            with channel.makefile("rwb") as stream:

                def exchange(action, identity, **extra):
                    stream.write(
                        json.dumps(
                            {
                                "token": record["token"],
                                "action": action,
                                "operation_id": identity,
                                **extra,
                            }
                        ).encode()
                        + b"\n"
                    )
                    stream.flush()
                    return json.loads(stream.readline())

                outcome = exchange("execute", operation_id, payload=payload)
                assert outcome["disposal_error"] == "OSError"
                assert outcome["operation_response"]["status"] == 404
                assert exchange("recover", operation_id) == outcome
                assert exchange("ack", operation_id)["state"] == "acknowledged"
                refused = exchange("execute", str(uuid.uuid4()), payload=payload)
                assert refused["owner_error"] == "OperationOutcomeUnresolved"
        event = json.loads((directory / "failures.ndjson").read_text())
        assert event["operation_id"] == operation_id
        assert event["response"] == outcome["operation_response"]
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            process.stderr.close()
        if record is not None:
            assert record["pid"] == process.pid
            socket_path = Path(record["socket"])
            socket_info = socket_path.lstat()
            assert [socket_info.st_dev, socket_info.st_ino] == record["socket_identity"]
            directory_info = socket_path.parent.lstat()
            assert [directory_info.st_dev, directory_info.st_ino] == record[
                "socket_dir_identity"
            ]
            socket_path.unlink()
            socket_path.parent.rmdir()
