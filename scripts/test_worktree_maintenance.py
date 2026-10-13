"""Maintenance routing and launcher regressions; no services or database fixtures."""

from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from worktree_env import WorktreeError, load_identity, topology
import worktree_runtime as runtime

ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_TASKS = {
    "app.workers.cleanup.refresh_user_perf_mv",
    "app.workers.cleanup.refresh_audit_bi_mv",
    "app.workers.audit_partition.ensure_future_audit_partitions",
    "app.workers.audit_partition.archive_old_audit_partitions",
    "app.workers.prediction_partition.ensure_future_prediction_partitions",
}


class MaintenanceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is required")
    def test_compose_deployments_have_a_maintenance_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / "compose.env"
            # Run inside a sandbox copy of the compose files: Compose v2 (the
            # version CI runners ship) resolves `env_file:` against the project
            # directory and fails the whole `config` command when the file is
            # missing, while newer versions only warn. The production overlays
            # reference .env.production, which is gitignored and never present
            # in a fresh checkout, so stage an empty fixture beside the copies.
            for name in (
                "docker-compose.yml",
                "docker-compose.prod.yml",
                "docker-compose.lan-prod.yml",
            ):
                shutil.copy(ROOT / name, root / name)
            (root / ".env.production").write_text("")
            values = {
                "DATABASE_URL_DOCKER": "postgresql+asyncpg://runtime:fixture@postgres/dev",
                "MIGRATION_DATABASE_URL_DOCKER": "postgresql+asyncpg://owner:fixture@postgres/dev",
                "DATABASE_URL": "postgresql+asyncpg://runtime:fixture@postgres/prod",
                "MIGRATION_DATABASE_URL": "postgresql+asyncpg://owner:fixture@postgres/prod",
                "REDIS_URL": "redis://redis:6379/0",
                "MINIO_ENDPOINT": "minio:9000",
                "MINIO_ACCESS_KEY": "fixture",
                "MINIO_SECRET_KEY": "fixture-secret",
                "AAP_IMAGE_TAG": "fixture",
                "AAP_SHARED_NETWORK": "fixture",
                "AAP_PRODUCTION_STATE_DIR": temporary,
                "LAN_BIND_IP": "127.0.0.1",
            }
            env_file.write_text(
                "".join(f"{key}={value}\n" for key, value in values.items())
            )
            for files, owner_key in (
                (("docker-compose.yml",), "MIGRATION_DATABASE_URL_DOCKER"),
                (
                    ("docker-compose.yml", "docker-compose.prod.yml"),
                    "MIGRATION_DATABASE_URL",
                ),
                (("docker-compose.lan-prod.yml",), None),
            ):
                with self.subTest(files=files):
                    command = ["docker", "compose", "--env-file", str(env_file)]
                    for file in files:
                        command.extend(["-f", str(root / file)])
                    result = subprocess.run(
                        [*command, "config", "--no-env-resolution", "--format", "json"],
                        env={
                            key: os.environ[key]
                            for key in ("PATH", "HOME")
                            if key in os.environ
                        },
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    services = json.loads(result.stdout)["services"]
                    worker = services["celery-worker-maintenance"]
                    self.assertEqual(
                        worker["command"][worker["command"].index("-Q") + 1],
                        "maintenance",
                    )
                    for name, service in services.items():
                        if (
                            name.startswith("celery-worker")
                            and name != "celery-worker-maintenance"
                        ):
                            self.assertNotIn("maintenance", service["command"])
                    self.assertIn("healthcheck", worker)
                    if owner_key:
                        self.assertEqual(
                            worker["environment"]["DATABASE_URL"], values[owner_key]
                        )
                        self.assertEqual(
                            worker["environment"]["MIGRATION_DATABASE_URL"], ""
                        )
                        ordinary = services["celery-worker"]["environment"]
                        self.assertEqual(ordinary["MIGRATION_DATABASE_URL"], "")
                        self.assertEqual(ordinary["ALEMBIC_AUTO_UPGRADE"], "false")
                        self.assertEqual(
                            ordinary["DATABASE_URL"],
                            values[owner_key.replace("MIGRATION_", "")],
                        )
                        self.assertIn(
                            "celery-worker-maintenance",
                            services["celery-worker"]["depends_on"],
                        )
                        self.assertEqual(
                            worker["environment"]["ALEMBIC_AUTO_UPGRADE"],
                            "false",
                        )
                    else:
                        self.assertNotIn("secrets", worker)
                        self.assertNotIn("DATABASE_URL", worker["environment"])

    def test_only_owner_required_tasks_route_to_maintenance(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "from app.workers.celery_app import celery_app; import json; print(json.dumps(celery_app.conf.task_routes))",
            ],
            cwd=ROOT / "apps/api",
            capture_output=True,
            text=True,
            check=True,
        )
        routes = json.loads(result.stdout)
        self.assertEqual(
            {name for name, route in routes.items() if route["queue"] == "maintenance"},
            MAINTENANCE_TASKS,
        )
        self.assertEqual(
            routes["app.workers.cleanup.purge_soft_deleted_attachments"]["queue"],
            "cleanup",
        )

    def test_workers_separate_credentials_and_shutdown_as_one_environment(self):
        for failure in (None, "wrong_queue", "worker_exit"):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                resources = topology(root, load_identity(root, create=True), "test")
                env = {
                    "DATABASE_URL": "runtime-fixture",
                    "MIGRATION_DATABASE_URL": "owner-fixture",
                    "TEST_DATABASE_URL": "owner-fixture",
                    "PLAYWRIGHT_E2E_DATABASE_URL": "owner-fixture",
                    "CELERY_BROKER_URL": "redis://127.0.0.1:6391/0",
                }
                backend = Mock(resources=resources, code_root=ROOT)
                backend.environment.return_value = env
                workers = [Mock(pid=101), Mock(pid=102)]
                for worker in workers:
                    worker.poll.return_value = None
                supervisor = MagicMock(pid=103, returncode=0)
                supervisor.poll.return_value = 0
                if failure == "worker_exit":
                    # Alive through readiness, then dies while the command is running.
                    workers[1].poll.side_effect = [None, 1, 1]
                    supervisor.poll.side_effect = [None, 0]
                names = [
                    f"worktree-{resources['id']}-test{suffix}@local"
                    for suffix in ("", "-maintenance")
                ]
                queues = [
                    "default,media,cleanup,audit,export,image-pyramid,ml.cpu",
                    "maintenance",
                ]
                client = Mock()

                def inspect(*, destination, timeout):
                    index = names.index(destination[0])
                    inspector = Mock()
                    inspector.registered.return_value = {
                        names[index]: [
                            "app.workers.audit.persist_audit_entry",
                            *MAINTENANCE_TASKS,
                        ]
                    }
                    active = queues[index].split(",")
                    if index == 1 and failure == "wrong_queue":
                        active.append("default")
                    inspector.active_queues.return_value = {
                        names[index]: [{"name": name} for name in active]
                    }
                    return inspector

                client.control.inspect.side_effect = inspect
                published = []
                with (
                    patch.object(
                        runtime.subprocess,
                        "Popen",
                        side_effect=lambda command, **kwargs: (
                            supervisor
                            if command[0] == "node"
                            else workers[int("--queues=maintenance" in command)]
                        ),
                    ) as popen,
                    patch.object(
                        runtime,
                        "process_handle",
                        side_effect=lambda pid, scope: {"pid": pid, "scope": scope},
                    ),
                    patch.object(
                        runtime,
                        "atomic_json",
                        side_effect=lambda path, value: published.append(
                            deepcopy(value)
                        ),
                    ),
                    patch.object(runtime.os, "killpg") as kill,
                    patch("celery.Celery", return_value=client),
                    patch.object(
                        runtime.time,
                        "monotonic",
                        side_effect=[0, 0, 0, 0, 61, 62, 62, 62, 62],
                    ),
                ):
                    options = runtime.parse_arguments(["--with-worker"])
                    if failure:
                        with self.assertRaises(WorktreeError):
                            runtime.run_processes(backend, options)
                    else:
                        self.assertEqual(runtime.run_processes(backend, options), 0)
                self.assertEqual(popen.call_count, 2 if failure == "wrong_queue" else 3)
                for index, (call, expected_db) in enumerate(
                    zip(popen.call_args_list, ("runtime-fixture", "owner-fixture"))
                ):
                    self.assertIn(f"--queues={queues[index]}", call.args[0])
                    self.assertEqual(call.kwargs["env"]["DATABASE_URL"], expected_db)
                    for key in (
                        "MIGRATION_DATABASE_URL",
                        "TEST_DATABASE_URL",
                        "PLAYWRIGHT_E2E_DATABASE_URL",
                    ):
                        self.assertEqual(call.kwargs["env"][key], "")
                for worker in workers:
                    worker.wait.assert_called_once()
                client.close.assert_called_once()
                self.assertIn(101, [call.args[0] for call in kill.call_args_list])
                if failure is None:
                    api_env = json.loads(
                        supervisor.stdin.__enter__.return_value.write.call_args.args[0]
                    )
                    self.assertEqual(api_env["DATABASE_URL"], "runtime-fixture")
                    self.assertEqual(api_env["MIGRATION_DATABASE_URL"], "")
                    self.assertEqual(published[-1]["worker"], names[0])
                    self.assertEqual(published[-1]["maintenance_worker"], names[1])
                    self.assertEqual(
                        {child["role"] for child in published[-1]["children"]},
                        {"worker", "maintenance_worker", "supervisor"},
                    )


if __name__ == "__main__":
    unittest.main()
