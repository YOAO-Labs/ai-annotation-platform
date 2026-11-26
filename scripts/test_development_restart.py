"""Check development restart boundaries without touching Docker or host services."""

import argparse
import copy
import json
import tempfile
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "dev_restart", Path(__file__).with_name("development-restart.py")
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class DevelopmentRestartTests(unittest.TestCase):
    def setUp(self):
        self.root = Path("/checkout")
        self.containers = [
            dict(
                Id=s,
                State={"Status": "running"},
                Config={
                    "Env": ["ALEMBIC_AUTO_UPGRADE=false"],
                    "Labels": {
                        "com.docker.compose.project": "ai-annotation-platform",
                        "com.docker.compose.project.working_dir": str(self.root),
                        "com.docker.compose.project.config_files": "/checkout/docker-compose.yml",
                        "com.docker.compose.service": s,
                        "com.docker.compose.oneoff": "False",
                    },
                },
            )
            for s in m.WORKERS
        ]
        self.recovery = next(
            c for c in self.containers if c["Id"] == "celery-worker-jobs-recovery"
        )["Config"]
        self.recovery["Cmd"] = [
            "/bin/sh",
            "-ec",
            "python -m app.workers.jobs_dispatch; "
            "exec celery -A app.workers.celery_app worker -l info -Q jobs.recovery "
            "--concurrency=1 --prefetch-multiplier=1",
        ]
        self.recovery["Env"] += [
            "JOBS_DISPATCH_DATABASE_URL_FILE=/run/secrets/jobs_dispatch_database_url",
            "JOBS_PUBLICATION_DATABASE_URL_FILE=/run/secrets/jobs_publication_database_url",
        ]

    def test_only_development_workers_selected(self):
        extra = [
            dict(
                Id=s,
                State={"Status": "running"},
                Config={
                    "Env": ["ALEMBIC_AUTO_UPGRADE=false"],
                    "Labels": {"com.docker.compose.service": s},
                },
            )
            for s in (
                "postgres",
                "minio",
                "redis",
                "api",
                "web",
                "gateway",
                "yolo-backend",
                "assets-images-worker",
                "identity-issuer",
            )
        ]
        self.assertEqual(
            m.validate(self.containers + extra, self.root), list(m.WORKERS)
        )

    def test_inference_worker_dev_overlay_keeps_worker_identity_valid(self):
        labels = self.containers[0]["Config"]["Labels"]
        labels["com.docker.compose.project.config_files"] += (
            ",/checkout/docker-compose.inference-workers-development.yml"
        )
        self.assertEqual(m.validate(self.containers, self.root), list(m.WORKERS))

    def test_production_worker_refused(self):
        self.containers[0]["Config"]["Labels"]["com.docker.compose.project"] = (
            "aap-production"
        )
        with self.assertRaises(RuntimeError):
            m.validate(self.containers, self.root)

    def test_other_checkout_refused(self):
        with self.assertRaises(RuntimeError):
            m.validate(self.containers, Path("/other"))

    def test_missing_worker_refused(self):
        with self.assertRaises(RuntimeError):
            m.validate(self.containers[:-1], self.root)

    def test_missing_jobs_recovery_refused(self):
        without_recovery = [
            c for c in self.containers if c["Id"] != "celery-worker-jobs-recovery"
        ]
        with self.assertRaisesRegex(
            RuntimeError, "Missing.*celery-worker-jobs-recovery"
        ):
            m.validate(without_recovery, self.root)

    def test_jobs_recovery_rejects_wrong_queue_and_missing_credential(self):
        command = self.recovery["Cmd"]
        self.recovery["Cmd"] = [
            *command[:2],
            command[2].replace("jobs.recovery", "default"),
        ]
        with self.assertRaisesRegex(RuntimeError, "Jobs recovery configuration"):
            m.validate(self.containers, self.root)
        self.recovery["Cmd"] = command
        self.recovery["Env"].pop()
        with self.assertRaisesRegex(RuntimeError, "Jobs recovery configuration"):
            m.validate(self.containers, self.root)

    def test_claim_credential_not_allowed_on_ordinary_workers(self):
        self.containers[0]["Config"]["Env"].append(
            "JOBS_DISPATCH_DATABASE_URL_FILE=/run/secrets/jobs_dispatch_database_url"
        )
        with self.assertRaisesRegex(RuntimeError, "Jobs claim credential"):
            m.validate(self.containers, self.root)

    def test_duplicate_worker_refused(self):
        with self.assertRaises(RuntimeError):
            m.validate(self.containers + [copy.deepcopy(self.containers[0])], self.root)

    def test_production_overlay_refused(self):
        self.containers[0]["Config"]["Labels"][
            "com.docker.compose.project.config_files"
        ] += ",/checkout/docker-compose.prod.yml"
        with self.assertRaises(RuntimeError):
            m.validate(self.containers, self.root)

    def test_auto_migrating_container_refused(self):
        self.containers[0]["Config"]["Env"] = ["ALEMBIC_AUTO_UPGRADE=true"]
        with self.assertRaisesRegex(RuntimeError, "migration"):
            m.validate(self.containers, self.root)

    def test_implicit_migration_default_refused(self):
        self.containers[0]["Config"]["Env"] = []
        with self.assertRaisesRegex(RuntimeError, "migration"):
            m.validate(self.containers, self.root)

    def test_maintenance_during_worker_restart_prevents_host_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            args = argparse.Namespace(
                project_dir=self.root, state_dir=state_dir, check=False
            )

            def docker(*args, **kwargs):
                if args[0] == "ps":
                    return " ".join(m.WORKERS)
                if args[0] == "inspect":
                    return json.dumps(self.containers)
                if args[0] == "restart":
                    (state_dir / "maintenance").touch()
                    return ""
                raise AssertionError(args)

            with (
                patch.object(m, "docker", docker),
                patch.object(m, "command") as command,
                patch.object(m, "http_ok", return_value=True),
            ):
                m.run(args)
            command.assert_not_called()

    def test_docker_context_is_explicit(self):
        with patch.object(m, "command", return_value="ok") as command:
            self.assertEqual(m.docker("ps"), "ok")
        command.assert_called_once_with(
            "docker", "--context", "default", "ps", timeout=30
        )


if __name__ == "__main__":
    unittest.main()
