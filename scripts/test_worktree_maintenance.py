"""Maintenance routing and launcher regressions; no services or database fixtures."""

from copy import deepcopy
from itertools import count
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
    "app.workers.dataset_import_delivery.relay_dataset_import_deliveries",
    "app.workers.predictions_retry.relay_prediction_retry_deliveries",
}


class MaintenanceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is required")
    def test_compose_deployments_have_a_maintenance_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / "compose.env"
            for name in (
                "docker-compose.yml",
                "docker-compose.prod.yml",
                "docker-compose.lan-prod.yml",
            ):
                shutil.copy(ROOT / name, root / name)
            (root / ".env.production").write_text("")
            insights_refresh = root / "insights-refresh-database-url"
            insights_refresh.write_text(
                "postgresql://insights_refresh:fixture@postgres/prod\n",
                encoding="utf-8",
            )
            values = {
                "DATABASE_URL_DOCKER": "postgresql+asyncpg://runtime:fixture@postgres/dev",
                "MIGRATION_DATABASE_URL_DOCKER": "postgresql+asyncpg://owner:fixture@postgres/dev",
                "DATABASE_URL": "postgresql+asyncpg://runtime:fixture@postgres/prod",
                "MIGRATION_DATABASE_URL": "postgresql+asyncpg://owner:fixture@postgres/prod",
                "REDIS_URL": "redis://redis:6379/0",
                "MINIO_ENDPOINT": "minio:9000",
                "MINIO_ACCESS_KEY": "fixture",
                "MINIO_SECRET_KEY": "fixture-secret",
                "MINIO_TRAINING_BUCKET": "fixture-training",
                "MINIO_INPUTS_BUCKET": "fixture-accepted-inputs",
                "AAP_IMAGE_TAG": "fixture",
                "AAP_SHARED_NETWORK": "fixture",
                "AAP_PRODUCTION_STATE_DIR": temporary,
                "LAN_BIND_IP": "127.0.0.1",
                "INSIGHTS_REFRESH_DATABASE_URL_FILE": str(insights_refresh),
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
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
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
                        self.assertEqual(
                            worker["environment"]["MINIO_TRAINING_BUCKET"],
                            values["MINIO_TRAINING_BUCKET"],
                        )
                        self.assertEqual(
                            worker["environment"]["MINIO_INPUTS_BUCKET"],
                            values["MINIO_INPUTS_BUCKET"],
                        )

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
        self.assertFalse(
            any(name.startswith("app.workers.identity_mail") for name in routes)
        )
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
                dispatch = "postgresql://dispatch:fixture@postgres/test"
                publication = "postgresql://publication:fixture@postgres/test"
                assets_env = {
                    f"ASSETS_CELERY_{lane.upper()}_DATABASE_URL": (
                        f"postgresql+asyncpg://assets_{lane}:fixture@postgres/test"
                    )
                    for lane in ("video", "image")
                }
                env = {
                    "DATABASE_URL": "runtime-fixture",
                    "MIGRATION_DATABASE_URL": "owner-fixture",
                    "TEST_DATABASE_URL": "owner-fixture",
                    "PLAYWRIGHT_E2E_DATABASE_URL": "owner-fixture",
                    "CELERY_BROKER_URL": "redis://127.0.0.1:6391/0",
                    "JOBS_PUBLICATION_DATABASE_URL": publication,
                    "SMTP_PASSWORD": "ordinary-config-fixture",
                }
                backend = Mock(resources=resources, code_root=ROOT)
                backend.environment.return_value = env
                backend.jobs_database_url.return_value = dispatch
                backend.assets_worker_environment.return_value = assets_env
                workers = [Mock(pid=101), Mock(pid=102), Mock(pid=103)]
                beat = Mock(pid=104)
                for worker in (*workers, beat):
                    worker.poll.return_value = None
                supervisor = MagicMock(pid=105, returncode=0)
                supervisor.poll.return_value = 0
                if failure == "worker_exit":
                    workers[1].poll.side_effect = [None, 1, 1]
                    supervisor.poll.side_effect = [None, 0]
                names = [
                    f"worktree-{resources['id']}-test{suffix}@local"
                    for suffix in ("", "-maintenance", "-jobs-recovery")
                ]
                queues = [
                    "default,media,cleanup,audit,export,image-pyramid,ml.cpu",
                    "maintenance",
                    "jobs.recovery",
                ]
                registered_tasks = (
                    [
                        "app.workers.audit.persist_audit_entry",
                        "app.workers.tasks.batch_predict",
                    ],
                    ["app.workers.audit_partition.ensure_future_audit_partitions"],
                    ["app.workers.jobs_dispatch.recover_job_dispatches"],
                )
                client = Mock()

                def inspect(*, destination, timeout):
                    index = names.index(destination[0])
                    inspector = Mock()
                    inspector.registered.return_value = {
                        names[index]: registered_tasks[index]
                    }
                    active = queues[index].split(",")
                    if index == 1 and failure == "wrong_queue":
                        active.append("default")
                    inspector.active_queues.return_value = {
                        names[index]: [{"name": name} for name in active]
                    }
                    return inspector

                def launch(command, **_kwargs):
                    if command[0] == "node":
                        return supervisor
                    if command[1] == "-c":
                        self.assertIn("recover-job-dispatches", command[2])
                        return beat
                    return next(
                        worker
                        for queue, worker in zip(queues, workers, strict=True)
                        if f"--queues={queue}" in command
                    )

                client.control.inspect.side_effect = inspect
                published = []
                with (
                    patch.object(
                        runtime.subprocess,
                        "Popen",
                        side_effect=launch,
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
                        side_effect=count(step=30),
                    ),
                ):
                    options = runtime.parse_arguments(["--with-worker"])
                    if failure:
                        with self.assertRaises(WorktreeError):
                            runtime.run_processes(backend, options)
                    else:
                        self.assertEqual(runtime.run_processes(backend, options), 0)
                self.assertEqual(popen.call_count, 2 if failure == "wrong_queue" else 5)
                backend.jobs_database_url.assert_called_once_with("jobs-dispatch")
                for key, url in assets_env.items():
                    self.assertEqual(popen.call_args_list[0].kwargs["env"][key], url)
                    for other in popen.call_args_list[1:]:
                        self.assertFalse(other.kwargs["env"].get(key), key)
                for index, expected_db in enumerate(
                    ("runtime-fixture", "owner-fixture")
                ):
                    call = popen.call_args_list[index]
                    self.assertIn(f"--queues={queues[index]}", call.args[0])
                    self.assertEqual(call.kwargs["env"]["DATABASE_URL"], expected_db)
                    for key in (
                        "MIGRATION_DATABASE_URL",
                        "TEST_DATABASE_URL",
                        "PLAYWRIGHT_E2E_DATABASE_URL",
                    ):
                        self.assertEqual(call.kwargs["env"][key], "")
                    self.assertFalse(
                        call.kwargs["env"].get("JOBS_DISPATCH_DATABASE_URL")
                    )
                self.assertEqual(
                    popen.call_args_list[0].kwargs["env"][
                        "JOBS_PUBLICATION_DATABASE_URL"
                    ],
                    publication,
                )
                self.assertFalse(
                    popen.call_args_list[1].kwargs["env"][
                        "JOBS_PUBLICATION_DATABASE_URL"
                    ]
                )
                if failure != "wrong_queue":
                    recovery, interval = popen.call_args_list[2:4]
                    self.assertIn("--queues=jobs.recovery", recovery.args[0])
                    self.assertEqual(
                        recovery.kwargs["env"]["JOBS_DISPATCH_DATABASE_URL"], dispatch
                    )
                    self.assertEqual(
                        recovery.kwargs["env"]["JOBS_PUBLICATION_DATABASE_URL"],
                        publication,
                    )
                    self.assertEqual(
                        recovery.kwargs["env"]["DATABASE_URL"],
                        "postgresql+asyncpg://dispatch:fixture@postgres/test",
                    )
                    for process in (recovery, interval):
                        process_env = process.kwargs["env"]
                        self.assertEqual(
                            process_env["CELERY_BROKER_URL"], env["CELERY_BROKER_URL"]
                        )
                        self.assertFalse(process_env.get("SMTP_PASSWORD"))
                        self.assertFalse(process_env.get("MIGRATION_DATABASE_URL"))
                    for key in (
                        "JOBS_DISPATCH_DATABASE_URL",
                        "JOBS_PUBLICATION_DATABASE_URL",
                    ):
                        self.assertFalse(interval.kwargs["env"].get(key))
                    beat.wait.assert_called_once()
                started_workers = workers[:2] if failure == "wrong_queue" else workers
                for worker in started_workers:
                    worker.wait.assert_called_once()
                client.close.assert_called_once()
                self.assertIn(101, [call.args[0] for call in kill.call_args_list])
                if failure is None:
                    api_env = json.loads(
                        supervisor.stdin.__enter__.return_value.write.call_args.args[0]
                    )
                    self.assertEqual(api_env["DATABASE_URL"], "runtime-fixture")
                    self.assertEqual(api_env["MIGRATION_DATABASE_URL"], "")
                    for key in assets_env:
                        self.assertFalse(api_env.get(key), key)
                    self.assertEqual(published[-1]["worker"], names[0])
                    self.assertEqual(published[-1]["maintenance_worker"], names[1])
                    self.assertEqual(published[-1]["jobs_recovery_worker"], names[2])
                    self.assertEqual(published[-1]["jobs_recovery_beat"], beat.pid)
                    self.assertEqual(
                        {child["role"] for child in published[-1]["children"]},
                        {
                            "worker",
                            "maintenance_worker",
                            "jobs_recovery_worker",
                            "jobs_recovery_beat",
                            "supervisor",
                        },
                    )


if __name__ == "__main__":
    unittest.main()
