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
                        "com.docker.compose.project.config_files": "/checkout/docker-compose.yml,/checkout/docker-compose.ml.yml",
                        "com.docker.compose.service": s,
                        "com.docker.compose.oneoff": "False",
                    },
                },
            )
            for s in m.WORKERS
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
            )
        ]
        self.assertEqual(
            m.validate(self.containers + extra, self.root), list(m.WORKERS)
        )

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
