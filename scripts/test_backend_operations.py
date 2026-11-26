"""Focused, DB-free maintenance tests. Docker commands are always mocked."""

from __future__ import annotations

from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = ROOT / ".test-yoao-plugins"
sys.path.insert(0, str(ROOT / "apps/api"))
from app.services.backend_operations import JobStore, OperationConflict, TargetChanged  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "backend_runner", ROOT / "scripts/ml-backend-runner.py"
)
runner_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner_module)


class FakeRunner(runner_module.Runner):
    def __init__(self, store):
        super().__init__(
            ROOT,
            "maintenance-test",
            [
                ROOT / "docker-compose.yml",
                PLUGINS_ROOT / "plugins/inference/yolo/docker-compose.yml",
            ],
            store,
            plugins_root=PLUGINS_ROOT,
        )
        self.commands = []
        self.failure = None
        self.image_ref = "registry.invalid/yolo@sha256:" + "a" * 64
        self.image_id = "sha256:" + "b" * 64
        self.lifecycle_hash = "c" * 64
        self.container = {
            "Id": "old-id",
            "Image": self.image_id,
            "Config": {
                "Image": self.image_ref,
                "Entrypoint": [
                    "uvicorn",
                    "main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "8003",
                    "--workers",
                    "1",
                ],
                "Cmd": None,
                "Env": [],
                "Labels": {
                    "com.docker.compose.project": self.project,
                    "com.docker.compose.service": "yolo-backend",
                    "com.docker.compose.project.working_dir": str(ROOT),
                    "com.docker.compose.project.config_files": ",".join(
                        str(p) for p in self.files
                    ),
                    runner_module.LIFECYCLE_HASH_LABEL: self.lifecycle_hash,
                    "com.docker.compose.oneoff": "False",
                },
            },
            "State": {
                "Status": "running",
                "Health": {"Status": "healthy"},
                "ExitCode": 0,
            },
            "Mounts": [
                {
                    "Destination": "/app/checkpoints",
                    "Type": "volume",
                    "Name": "maintenance-test_yolo_checkpoints",
                    "Source": "/private/volume",
                    "RW": True,
                },
                {
                    "Destination": "/model-assets",
                    "Type": "bind",
                    "Source": "/private/model-assets",
                    "RW": False,
                },
            ],
        }
        self.probe = {
            "argv": list(self.container["Config"]["Entrypoint"]),
            "shutdown": True,
            "lifecycle_sha256": self.lifecycle_hash,
        }
        self.services = [{"provider": "yolo", "can_maintain": True, "reason": None}]
        self.heartbeat()

    def command(self, args, **kwargs):
        self.commands.append(args)
        if "image" in args and "inspect" in args:
            return json.dumps([{"Id": self.image_id, "RepoDigests": [self.image_ref]}])
        if "config" in args:
            provider = next(
                p
                for p, service in runner_module.PROVIDERS.items()
                if service
                == self.container["Config"]["Labels"]["com.docker.compose.service"]
            )
            profile = "gpu" if provider == "gsam2" else f"gpu-{provider}"
            if profile not in args:
                return json.dumps({"services": {}})
            return json.dumps(
                {
                    "services": {
                        runner_module.PROVIDERS[provider]: {
                            "image": self.image_ref,
                            "labels": {
                                runner_module.LIFECYCLE_HASH_LABEL: self.lifecycle_hash
                            },
                        }
                    }
                }
            )
        if "run" in args:
            digest = "0" * 64 if self.failure == "target_hash" else self.lifecycle_hash
            return json.dumps({"shutdown": True, "lifecycle_sha256": digest})
        if "inspect" in args:
            return json.dumps([self.container])
        if "ps" in args:
            return self.container["Id"]
        if "exec" in args:
            return json.dumps(self.probe)
        if "pull" in args and self.failure == "pull":
            raise runner_module.CommandFailure()
        if "stop" in args:
            if self.failure == "stop":
                raise runner_module.TrackingTimeout()
            self.container["State"]["Status"] = "exited"
        if "up" in args:
            self.container["Id"] = "new-id"
            self.container["State"]["Status"] = "running"
            if self.failure == "mount":
                self.container["Mounts"][0]["Name"] = "foreign-volume"
        return ""

    def job(self):
        return self.store.publish(
            self.store.reserve(
                "yolo",
                expected_deployment=self.runner.project
                if hasattr(self, "runner")
                else self.project,
                expected_runner_id=self.runner.runner_id
                if hasattr(self, "runner")
                else self.runner_id,
            )["id"]
        )


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "queue")
        self.runner = FakeRunner(self.store)

    def test_private_atomic_queue_and_duplicate_conflict(self):
        job = self.store.reserve(
            "yolo",
            expected_deployment=self.runner.project
            if hasattr(self, "runner")
            else self.project,
            expected_runner_id=self.runner.runner_id
            if hasattr(self, "runner")
            else self.runner_id,
        )
        self.assertFalse(job["submitted"])
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            (self.store.directory / f"{job['id']}.json").stat().st_mode & 0o777, 0o600
        )
        with self.assertRaises(OperationConflict):
            self.store.reserve(
                "yolo",
                expected_deployment=self.runner.project
                if hasattr(self, "runner")
                else self.project,
                expected_runner_id=self.runner.runner_id
                if hasattr(self, "runner")
                else self.runner_id,
            )
        self.assertFalse(self.store.status()["services"][2]["can_maintain"])
        self.store.abort_reservation(job["id"])
        self.assertEqual(self.store.jobs(), [])

    def test_confirmation_is_bound_to_runner_and_deployment(self):
        for deployment, runner_id in [
            ("another-project", self.runner.runner_id),
            (self.runner.project, "old-runner"),
        ]:
            with self.assertRaises(TargetChanged):
                self.store.reserve(
                    "yolo", expected_deployment=deployment, expected_runner_id=runner_id
                )
        self.assertEqual(self.store.jobs(), [])

    def test_missing_runner_and_undeployed_provider_are_disabled(self):
        status = self.store.status()
        self.assertTrue(
            next(s for s in status["services"] if s["provider"] == "yolo")[
                "can_maintain"
            ]
        )
        self.assertFalse(
            next(s for s in status["services"] if s["provider"] == "sam3")[
                "can_maintain"
            ]
        )
        heartbeat = self.store.read("heartbeat.json")
        heartbeat["timestamp"] = time.time() - 20
        self.store.write("heartbeat.json", heartbeat)
        self.assertFalse(self.store.status()["available"])

    def test_malformed_and_symlink_queue_fail_closed(self):
        path = self.store.directory / f"{uuid.uuid4()}.json"
        path.write_text('{"provider": []}')
        path.chmod(0o600)
        self.assertFalse(self.store.status()["available"])
        path.unlink()
        (self.store.directory / "queue.lock").symlink_to("heartbeat.json")
        with self.assertRaises(OSError):
            with self.store.lock():
                pass

    def test_fifo_lock_never_blocks(self):
        os.mkfifo(self.store.directory / "queue.lock", 0o600)
        with self.assertRaises(PermissionError):
            with self.store.lock():
                pass

    def test_malformed_heartbeat_and_job_metadata_fail_closed(self):
        heartbeat = self.store.read("heartbeat.json")
        heartbeat["services"] = [{"provider": [], "can_maintain": True}]
        self.store.write("heartbeat.json", heartbeat)
        self.assertFalse(self.store.status()["available"])
        self.runner.heartbeat()
        job = self.runner.job()
        job["error"] = []
        self.store.write(f"{job['id']}.json", job)
        self.assertFalse(self.store.status()["available"])

    def test_pull_before_infinite_grace_stop_and_volume_preservation(self):
        job = self.runner.job()
        self.runner.run_job(job)
        result = self.store.read(f"{job['id']}.json")
        self.assertEqual(result["status"], "succeeded")
        actions = [
            args
            for args in self.runner.commands
            if "pull" in args or "stop" in args or "up" in args
        ]
        self.assertIn("pull", actions[0])
        self.assertEqual(
            actions[1][-6:],
            ["stop", "--signal", "SIGTERM", "--timeout", "-1", "old-id"],
        )
        self.assertEqual(
            actions[2][-6:],
            ["up", "-d", "--no-deps", "--no-build", "--force-recreate", "yolo-backend"],
        )
        self.assertNotIn("auth", json.dumps(result))
        self.assertFalse(
            any(
                "kill" in args or "down" in args or "-v" in args
                for args in self.runner.commands
            )
        )

    def test_pull_failure_does_not_stop_old_container(self):
        self.runner.failure = "pull"
        job = self.runner.job()
        self.runner.run_job(job)
        self.assertEqual(self.store.read(f"{job['id']}.json")["status"], "failed")
        self.assertFalse(
            any("stop" in args or "up" in args for args in self.runner.commands)
        )

    def test_sam_plugins_require_reviewed_image_before_stopping(self):
        for provider, port in (("gsam2", 8001), ("sam3", 8002)):
            with self.subTest(provider=provider):
                runner = FakeRunner(self.store)
                runner.services = [
                    {"provider": provider, "can_maintain": True, "reason": None}
                ]
                runner.heartbeat()
                runner.container["Config"]["Labels"]["com.docker.compose.service"] = (
                    runner_module.PROVIDERS[provider]
                )
                runner.container["Config"]["Entrypoint"][5] = str(port)
                runner.probe["argv"][5] = str(port)
                runner.container["Mounts"] = [
                    {
                        "Destination": path,
                        "Type": "volume",
                        "Name": path,
                        "Source": path,
                        "RW": True,
                    }
                    for path in ("/app/checkpoints", "/app/.cache/huggingface")
                ]
                runner.failure = "target_hash"
                job = self.store.reserve(
                    provider,
                    expected_deployment=runner.project,
                    expected_runner_id=runner.runner_id,
                )
                runner.run_job(self.store.publish(job["id"]))
                self.assertEqual(
                    self.store.read(f"{job['id']}.json")["status"], "failed"
                )
                self.assertFalse(
                    any("stop" in args or "up" in args for args in runner.commands)
                )

    def test_unreviewed_target_lifecycle_never_stops_running_service(self):
        self.runner.failure = "target_hash"
        job = self.runner.job()
        self.runner.run_job(job)
        self.assertEqual(self.store.read(f"{job['id']}.json")["status"], "failed")
        self.assertFalse(
            any("stop" in args or "up" in args for args in self.runner.commands)
        )

    def test_tag_only_running_image_is_not_eligible_for_maintenance(self):
        self.runner.container["Config"]["Image"] = "registry.invalid/yolo:latest"
        with self.assertRaises(runner_module.UnsafeRuntime):
            self.runner.validate("yolo", self.runner.container)

    def test_stop_timeout_never_recreates_and_requires_host_inspection(self):
        self.runner.failure = "stop"
        job = self.runner.job()
        self.runner.run_job(job)
        result = self.store.read(f"{job['id']}.json")
        self.assertEqual(result["status"], "interrupted")
        self.assertTrue(result["requires_inspection"])
        self.assertFalse(any("up" in args for args in self.runner.commands))
        self.assertFalse(self.store.status()["services"][2]["can_maintain"])

    def test_changed_model_mount_fails_without_resume(self):
        self.runner.failure = "mount"
        job = self.runner.job()
        self.runner.run_job(job)
        self.assertTrue(self.store.read(f"{job['id']}.json")["requires_inspection"])

    def test_process_environment_or_lifecycle_mismatch_refuses(self):
        for key in ("identity", "environment", "pid", "hash"):
            with self.subTest(key=key):
                container = deepcopy(self.runner.container)
                probe = deepcopy(self.runner.probe)
                if key == "identity":
                    container["Config"]["Labels"]["com.docker.compose.project"] = (
                        "foreign"
                    )
                elif key == "environment":
                    container["Config"]["Env"] = ["UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN=1"]
                elif key == "pid":
                    self.runner.probe["argv"][-1] = "2"
                else:
                    self.runner.probe["lifecycle_sha256"] = "bad"
                with self.assertRaises(runner_module.UnsafeRuntime):
                    self.runner.validate("yolo", container)
                self.runner.probe = probe

    def test_restart_marks_running_uncertain_without_replay(self):
        job = self.runner.job()
        self.store.event(job, "stopping", "wait")
        self.runner.recover()
        self.assertTrue(self.store.read(f"{job['id']}.json")["requires_inspection"])
        self.runner.recover([job["id"]])
        self.assertFalse(
            self.store.read(f"{job['id']}.json").get("requires_inspection")
        )
        self.assertFalse(any("stop" in c for c in self.runner.commands))

    def test_runner_rejects_missing_or_unlisted_plugin_compose_files(self):
        with self.assertRaises(ValueError):
            runner_module.Runner(
                ROOT,
                "maintenance-test",
                [ROOT / "docker-compose.yml"],
                self.store,
                plugins_root=PLUGINS_ROOT,
            )
        with self.assertRaises(ValueError):
            runner_module.Runner(
                ROOT,
                "maintenance-test",
                [
                    ROOT / "docker-compose.yml",
                    PLUGINS_ROOT / "plugins/inference/unlisted/docker-compose.yml",
                ],
                self.store,
                plugins_root=PLUGINS_ROOT,
            )
        with self.assertRaises(ValueError):
            runner_module.Runner(
                ROOT,
                "maintenance-test",
                [
                    ROOT / "docker-compose.yml",
                    PLUGINS_ROOT / "plugins/inference/yolo/../../../docker-compose.yml",
                ],
                self.store,
                plugins_root=PLUGINS_ROOT,
            )
        with self.assertRaises(ValueError):
            runner_module.Runner(
                ROOT,
                "maintenance-test",
                [
                    ROOT / "docker-compose.yml",
                    PLUGINS_ROOT / "plugins/inference/yolo/docker-compose.yml",
                ],
                self.store,
                plugins_root="",
            )

    def test_runner_rejects_symlink_escape_and_invalid_cli_paths(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp)
            fake_plugins = tmp / "yoao-plugins"
            yolo_dir = fake_plugins / "plugins" / "inference" / "yolo"
            yolo_dir.mkdir(parents=True)
            outside_compose = tmp / "outside-compose.yml"
            outside_compose.write_text("services: {}\n", encoding="utf-8")
            escaped_compose = yolo_dir / "docker-compose.yml"
            escaped_compose.symlink_to(outside_compose)
            with self.assertRaises(ValueError):
                runner_module.Runner(
                    ROOT,
                    "maintenance-test",
                    [ROOT / "docker-compose.yml", escaped_compose],
                    self.store,
                    plugins_root=fake_plugins,
                )

            base_argv = [
                "ml-backend-runner.py",
                "--root",
                str(ROOT),
                "--project",
                "maintenance-test",
                "--operations-dir",
                str(tmp / "ops"),
            ]
            invalid_cases = [
                [
                    *base_argv,
                    "--plugins-root",
                    "",
                    "--compose-file",
                    str(ROOT / "docker-compose.yml"),
                ],
                [
                    *base_argv,
                    "--plugins-root",
                    str(tmp / "does-not-exist"),
                    "--compose-file",
                    str(ROOT / "docker-compose.yml"),
                ],
                [
                    *base_argv,
                    "--plugins-root",
                    str(fake_plugins),
                    "--compose-file",
                    str(ROOT / "docker-compose.yml"),
                    "--compose-file",
                    str(fake_plugins / "plugins/inference/sam3/docker-compose.yml"),
                ],
                [
                    *base_argv,
                    "--plugins-root",
                    str(fake_plugins),
                    "--compose-file",
                    str(ROOT / "docker-compose.yml"),
                    "--compose-file",
                    str(escaped_compose),
                ],
            ]
            for argv in invalid_cases:
                with patch.object(sys, "argv", argv):
                    with self.assertRaises(SystemExit) as ctx:
                        runner_module.main()
                    self.assertEqual(ctx.exception.code, 2)


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import httpx
        from fastapi import FastAPI
        from app.api.v1 import admin_backend_operations as api
        from app.deps import get_current_user, get_db

        self.api = api
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(Path(self.temp.name) / "queue")
        self.runner = FakeRunner(self.store)
        self.user = SimpleNamespace(role="super_admin", id=uuid.uuid4())
        self.payload = {
            "provider": "yolo",
            "expected_deployment": self.runner.project,
            "expected_runner_id": self.runner.runner_id,
        }
        self.db = SimpleNamespace(commit=AsyncMock())
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_current_user] = lambda: self.user
        app.dependency_overrides[get_db] = lambda: self.db
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.patcher = patch.object(api, "get_store", return_value=self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.audit = patch.object(api.AuditService, "log", new_callable=AsyncMock)
        self.audit_mock = self.audit.start()
        self.addCleanup(self.audit.stop)

    async def test_admin_submission_audited_and_extra_commands_rejected(self):
        response = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=self.payload
        )
        self.assertEqual(response.status_code, 202, response.text)
        data = response.json()
        self.assertNotIn("runner_id", data)
        self.assertNotIn("submitted", data)
        self.audit_mock.assert_awaited_once()
        self.db.commit.assert_awaited_once()
        self.assertEqual(
            (
                await self.client.get(f"/admin/ml-backend-operations/jobs/{data['id']}")
            ).status_code,
            200,
        )
        duplicate = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=self.payload
        )
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(
            (
                await self.client.post(
                    "/admin/ml-backend-operations/jobs",
                    json={"provider": "yolo", "command": "evil"},
                )
            ).status_code,
            422,
        )

    async def test_stale_confirmation_has_no_unrelated_job_id(self):
        payload = {**self.payload, "expected_runner_id": "old-runner"}
        response = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=payload
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["detail"]["error_code"], "backend_manager_target_changed"
        )
        self.assertNotIn("job_id", response.json()["detail"])
        self.assertEqual(self.store.jobs(), [])

    async def test_non_admin_cannot_read_or_submit(self):
        self.user.role = "employee"
        for method, path in [
            ("GET", "/status"),
            ("POST", "/jobs"),
            ("GET", f"/jobs/{uuid.uuid4()}"),
        ]:
            response = await self.client.request(
                method,
                "/admin/ml-backend-operations" + path,
                json=self.payload if method == "POST" else None,
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.store.jobs(), [])

    async def test_failed_audit_cannot_publish_runnable_job(self):
        self.db.commit.side_effect = RuntimeError("DB failed")
        response = await self.client.post(
            "/admin/ml-backend-operations/jobs", json=self.payload
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.store.jobs(), [])


if __name__ == "__main__":
    unittest.main()
