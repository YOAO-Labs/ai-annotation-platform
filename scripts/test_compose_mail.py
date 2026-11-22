import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = {
    "ENVIRONMENT": "production",
    "DATABASE_URL": "postgresql+asyncpg://runtime:fixture@postgres/fixture",
    "REDIS_URL": "redis://redis:6379/0",
    "MINIO_ENDPOINT": "minio:9000",
    "MINIO_ACCESS_KEY": "fixture",
    "MINIO_SECRET_KEY": "fixture-secret",
    "CORS_ALLOW_ORIGINS": '["https://app.example.test"]',
}
APP_PASSWORD = "/run/secrets/api_smtp_password"
ALERT_CONFIG = "/etc/alertmanager/alertmanager.yml"
ALERT_PASSWORD = "/run/secrets/alertmanager_smtp_password"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is required")
class ComposeMailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in ("docker-compose.yml", "docker-compose.prod.yml"):
            shutil.copy(ROOT / name, self.root / name)
        alert_config_dir = Path("infra/alertmanager")
        shutil.copytree(ROOT / alert_config_dir, self.root / alert_config_dir)

    def config(self, *, production=None, profile=None):
        values = production if production is not None else {}
        env_file = self.root / (
            ".env.production" if production is not None else ".env.development"
        )
        env_file.write_text(
            "".join(f"{name}={value}\n" for name, value in values.items()),
            encoding="utf-8",
        )
        command = [
            "docker",
            "compose",
            "--project-name",
            "compose-mail-regression",
            "--env-file",
            str(env_file),
            "-f",
            "docker-compose.yml",
        ]
        if production is not None:
            command.extend(["-f", "docker-compose.prod.yml"])
        if profile is not None:
            command.extend(["--profile", profile])
        result = subprocess.run(
            [*command, "config", "--format", "json"],
            cwd=self.root,
            env={
                k: os.environ[k]
                for k in ("HOME", "PATH", "DOCKER_CONFIG")
                if k in os.environ
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["services"]

    def assert_read_only_bind(self, service, target, source):
        mounts = [volume for volume in service["volumes"] if volume["target"] == target]
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["type"], "bind")
        self.assertEqual(mounts[0]["source"], str(source))
        self.assertIs(mounts[0]["read_only"], True)
        self.assertIs(mounts[0].get("bind", {}).get("create_host_path", False), False)

    def test_development_mailpit_and_monitoring_route(self):
        self.assertIn("mailpit", self.config())
        services = self.config(profile="monitoring")
        self.assertEqual(
            {p["target"] for p in services["mailpit"]["ports"]}, {1025, 8025}
        )
        source = self.root / "infra/alertmanager/alertmanager.yml"
        volumes = services["alertmanager"]["volumes"]
        self.assertEqual(
            next(v["source"] for v in volumes if v["target"] == ALERT_CONFIG),
            str(source),
        )
        self.assertIn(
            'smtp_smarthost: "mailpit:1025"', source.read_text(encoding="utf-8")
        )

    def test_default_production_excludes_mailpit_and_monitoring(self):
        services = self.config(production=PRODUCTION)
        self.assertNotIn("mailpit", services)
        self.assertNotIn("alertmanager", services)
        self.assertNotIn("SMTP_HOST", services["api"]["environment"])
        self.assertFalse(services["api"]["environment"]["SMTP_PASSWORD_FILE"])
        for name in ("postgres", "redis", "minio"):
            with self.subTest(service=name):
                self.assertFalse(services[name].get("ports"))
        for name, port in (("api", "8080"), ("web", "8088")):
            self.assertEqual(
                [(p["host_ip"], str(p["published"])) for p in services[name]["ports"]],
                [("127.0.0.1", port)],
            )

    def test_production_monitoring_is_notification_free_by_default(self):
        services = self.config(production=PRODUCTION, profile="*")
        self.assertNotIn("mailpit", services)
        source = self.root / "infra/alertmanager/alertmanager.production.yml"
        alertmanager = services["alertmanager"]
        self.assert_read_only_bind(alertmanager, ALERT_CONFIG, source)
        self.assertNotIn("extra_hosts", alertmanager)
        config = source.read_text(encoding="utf-8")
        self.assertIn("receiver: no-notifications", config)
        self.assertNotIn("email_configs:", config)
        self.assertNotIn("smtp_", config)

    def test_production_optional_mail_files_are_read_only_and_scoped(self):
        app_password = self.root / "app-password"
        alert_config = self.root / "alert-config.yml"
        alert_password = self.root / "alert-password"
        for path in (app_password, alert_config, alert_password):
            path.write_text("fixture-only", encoding="utf-8")
        services = self.config(
            production={
                **PRODUCTION,
                "SMTP_HOST": "smtp.example.test",
                "SMTP_PORT": "587",
                "SMTP_FROM": "robot@example.test",
                "SMTP_USER": "fixture",
                "SMTP_PASSWORD_FILE": str(app_password),
                "ALERTMANAGER_CONFIG_FILE": str(alert_config),
                "ALERTMANAGER_SMTP_PASSWORD_FILE": str(alert_password),
            },
            profile="monitoring",
        )
        for name in ("api", "celery-worker"):
            with self.subTest(service=name):
                service = services[name]
                self.assert_read_only_bind(service, APP_PASSWORD, app_password)
                self.assertEqual(
                    service["environment"]["SMTP_HOST"], "smtp.example.test"
                )
                self.assertEqual(
                    service["environment"]["SMTP_PASSWORD_FILE"], APP_PASSWORD
                )
                self.assertFalse(service["environment"]["SMTP_PASSWORD"])
        for name, service in services.items():
            if name.startswith("celery-") and name != "celery-worker":
                with self.subTest(service=name):
                    self.assertFalse(service["environment"]["SMTP_PASSWORD_FILE"])
                    self.assertNotIn(
                        APP_PASSWORD,
                        [v.get("target") for v in service.get("volumes", [])],
                    )
        self.assert_read_only_bind(services["alertmanager"], ALERT_CONFIG, alert_config)
        self.assert_read_only_bind(
            services["alertmanager"], ALERT_PASSWORD, alert_password
        )
        self.assertNotIn("fixture-only", json.dumps(services))


if __name__ == "__main__":
    unittest.main()
