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
JOBS_RECOVERY = "celery-worker-jobs-recovery"
JOBS_DISPATCH_FILE = "/run/secrets/jobs_dispatch_database_url"
JOBS_PUBLICATION_FILE = "/run/secrets/jobs_publication_database_url"
ALERT_CONFIG = "/etc/alertmanager/alertmanager.yml"
ALERT_PASSWORD = "/run/secrets/alertmanager_smtp_password"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is required")
class ComposeMailTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for name in (
            "docker-compose.yml",
            "docker-compose.prod.yml",
            "docker-compose.lan-prod.yml",
        ):
            shutil.copy(ROOT / name, self.root / name)
        alert_config_dir = Path("infra/alertmanager")
        shutil.copytree(ROOT / alert_config_dir, self.root / alert_config_dir)

    def config(self, *, production=None, lan=False, profile=None, variables=None):
        values = {**(production or {}), **(variables or {})}
        if lan:
            values = {
                "AAP_IMAGE_TAG": "fixture",
                "AAP_SHARED_NETWORK": "fixture-shared",
                "AAP_PRODUCTION_STATE_DIR": str(self.root),
                "LAN_BIND_IP": "127.0.0.1",
                "MINIO_TRAINING_BUCKET": "fixture-training",
                "MINIO_INPUTS_BUCKET": "fixture-inputs",
                **values,
            }
        env_file = self.root / (
            ".env.production" if production is not None or lan else ".env.development"
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
            "docker-compose.lan-prod.yml" if lan else "docker-compose.yml",
        ]
        if production is not None and not lan:
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
        api = services["api"]
        self.assertEqual(api["environment"]["SMTP_HOST"], "smtp.example.test")
        for name, service in services.items():
            if name == "api" or name.startswith("celery-"):
                with self.subTest(service=name):
                    self.assertFalse(
                        service.get("environment", {}).get("SMTP_PASSWORD_FILE")
                    )
                    self.assertFalse(
                        service.get("environment", {}).get("SMTP_PASSWORD")
                    )
                    self.assertNotIn(
                        str(app_password),
                        [v.get("source") for v in service.get("volumes", [])],
                    )
        self.assert_read_only_bind(services["alertmanager"], ALERT_CONFIG, alert_config)
        self.assert_read_only_bind(
            services["alertmanager"], ALERT_PASSWORD, alert_password
        )
        self.assertNotIn("fixture-only", json.dumps(services))

    def test_recovery_is_a_default_bare_worker_using_the_actual_broker(self):
        for deployment, production, lan in (
            ("base", None, False),
            ("prod", PRODUCTION, False),
            (
                "lan",
                {**PRODUCTION, "REDIS_URL": "redis://redis-production:6379/0"},
                True,
            ),
        ):
            with self.subTest(deployment=deployment):
                services = self.config(production=production, lan=lan)
                recovery = services[JOBS_RECOVERY]
                beat = services["celery-beat"]
                self.assertFalse(recovery.get("profiles"))
                self.assertFalse(recovery.get("env_file"))
                self.assertIn("healthcheck", recovery)
                self.assertEqual(
                    beat["depends_on"][JOBS_RECOVERY]["condition"], "service_healthy"
                )
                self.assertEqual(
                    recovery["command"],
                    [
                        "/bin/sh",
                        "-ec",
                        "python -m app.workers.jobs_dispatch; "
                        "exec celery -A app.workers.celery_app worker -l info "
                        "-Q jobs.recovery --concurrency=1 --prefetch-multiplier=1",
                    ],
                )
                self.assertEqual(
                    recovery["environment"],
                    {
                        "ALEMBIC_AUTO_UPGRADE": "false",
                        "CELERY_BROKER_URL": "",
                        "JOBS_DISPATCH_DATABASE_URL": "",
                        "JOBS_DISPATCH_DATABASE_URL_FILE": "",
                        "JOBS_PUBLICATION_DATABASE_URL": "",
                        "JOBS_PUBLICATION_DATABASE_URL_FILE": "",
                        "REDIS_URL": beat["environment"]["REDIS_URL"],
                    },
                )
                self.assert_read_only_bind(recovery, JOBS_DISPATCH_FILE, "/dev/null")
                self.assert_read_only_bind(recovery, JOBS_PUBLICATION_FILE, "/dev/null")
                if lan:
                    self.assertEqual(recovery["image"], "aap-production-api:fixture")
                    self.assertEqual(
                        set(recovery["networks"]), {"production", "shared"}
                    )
                    self.assertNotIn("build", recovery)
                    maintenance = services["celery-worker-maintenance"]["environment"]
                    self.assertEqual(
                        maintenance["DATABASE_URL"], production["DATABASE_URL"]
                    )
                    self.assertFalse(maintenance["MIGRATION_DATABASE_URL"])
                else:
                    self.assertEqual(
                        recovery["build"]["dockerfile"], "infra/docker/Dockerfile.api"
                    )

    def test_direct_recovery_dsns_and_broker_are_not_shared(self):
        dispatch = "postgresql+asyncpg://dispatch:fixture@postgres/fixture"
        publication = "postgresql+asyncpg://publication:fixture@postgres/fixture"
        broker = "redis://broker.example.test:6379/0"
        services = self.config(
            production={
                **PRODUCTION,
                "JOBS_DISPATCH_DATABASE_URL": dispatch,
                "JOBS_PUBLICATION_DATABASE_URL": publication,
                "CELERY_BROKER_URL": broker,
            }
        )
        recovery = services[JOBS_RECOVERY]
        self.assertEqual(
            recovery["environment"]["JOBS_DISPATCH_DATABASE_URL"], dispatch
        )
        self.assertEqual(
            recovery["environment"]["JOBS_PUBLICATION_DATABASE_URL"], publication
        )
        self.assertFalse(recovery["environment"]["JOBS_DISPATCH_DATABASE_URL_FILE"])
        self.assertFalse(recovery["environment"]["JOBS_PUBLICATION_DATABASE_URL_FILE"])
        self.assert_read_only_bind(recovery, JOBS_DISPATCH_FILE, "/dev/null")
        self.assert_read_only_bind(recovery, JOBS_PUBLICATION_FILE, "/dev/null")
        for name, service in services.items():
            if name != JOBS_RECOVERY:
                self.assertFalse(
                    service.get("environment", {}).get("JOBS_DISPATCH_DATABASE_URL")
                )
        for name in ("api", "celery-worker", "celery-beat", JOBS_RECOVERY):
            self.assertEqual(services[name]["environment"]["CELERY_BROKER_URL"], broker)

    def test_local_assets_workers_get_only_the_credentials_for_their_lane(self):
        video = "ASSETS_CELERY_VIDEO_DATABASE_URL_FILE"
        image = "ASSETS_CELERY_IMAGE_DATABASE_URL_FILE"
        media_workers = {"celery-worker", "celery-worker-cpu", "celery-worker-gpu"}
        owners = {
            video: media_workers,
            image: {*media_workers, "celery-worker-image-pyramid"},
        }
        sources = {key: self.root / key.lower() for key in owners}
        for source in sources.values():
            source.write_text("fixture-only", encoding="utf-8")

        for production, lan in ((None, False), (PRODUCTION, False), (PRODUCTION, True)):
            for active, video_mode, image_mode in (
                (video, "celery", "split"),
                (image, "split", "celery"),
            ):
                with self.subTest(lan=lan, video=video_mode, image=image_mode):
                    services = self.config(
                        production=production,
                        lan=lan,
                        variables={
                            "ASSETS_VIDEO_EXECUTOR": video_mode,
                            "ASSETS_IMAGE_EXECUTOR": image_mode,
                            active: str(sources[active]),
                        },
                    )
                    for name, service in services.items():
                        env = service.get("environment", {})
                        for key, allowed in owners.items():
                            target = f"/run/secrets/{key.removesuffix('_FILE').lower()}"
                            if name in allowed and key == active:
                                self.assertEqual(env[key], target, (name, key))
                                self.assert_read_only_bind(
                                    service, target, sources[key]
                                )
                            else:
                                self.assertFalse(env.get(key), (name, key))
                                if name in allowed:
                                    self.assert_read_only_bind(
                                        service, target, "/dev/null"
                                    )
                                else:
                                    mounts = service.get("volumes", [])
                                    self.assertNotIn(
                                        target, [v["target"] for v in mounts]
                                    )

    def test_production_capability_files_are_scoped_to_their_hosts(self):
        files = {
            "JOBS_DISPATCH_DATABASE_URL_FILE": (JOBS_DISPATCH_FILE, {JOBS_RECOVERY}),
            "JOBS_PUBLICATION_DATABASE_URL_FILE": (
                JOBS_PUBLICATION_FILE,
                {
                    "api",
                    "celery-worker",
                    "celery-worker-gpu",
                    "celery-worker-cpu",
                    "celery-worker-export",
                    JOBS_RECOVERY,
                },
            ),
            "NOTIFICATION_PUBLICATION_TOKEN_FILE": (
                "/run/secrets/notification_publication_token",
                {"api", "celery-worker", "celery-worker-export"},
            ),
            "NOTIFICATION_SUBSCRIBE_REDIS_URL_FILE": (
                "/run/secrets/notification_subscribe_redis_url",
                {"api"},
            ),
            "NOTIFICATION_PUBLISH_DATABASE_URL_FILE": (
                "/run/secrets/notification_publish_database_url",
                {"celery-worker"},
            ),
            "NOTIFICATION_PUBLISH_REDIS_URL_FILE": (
                "/run/secrets/notification_publish_redis_url",
                {"celery-worker"},
            ),
            "IDENTITY_EMBEDDED_SIGNING_SECRET_FILE": (
                "/run/secrets/identity_embedded_signing_secret",
                {"api"},
            ),
            "IDENTITY_RECOVERY_MAIL_KEY_FILE": (
                "/run/secrets/identity_recovery_mail_key",
                {"api"},
            ),
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_TOKEN_FILE": (
                "/run/secrets/assets_media_native_image_storage_token",
                {"api", "celery-worker"},
            ),
        }
        private_only = (
            "ASSETS_MEDIA_WORKER_DATABASE_URL_FILE",
            "ASSETS_IMAGES_DATABASE_URL_FILE",
            "JOBS_DISPATCH_RELAY_DATABASE_URL_FILE",
            "NOTIFICATION_PUBLISH_API_TOKEN_FILE",
            "NOTIFICATION_SOCKET_URL",
            "IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE",
            "IDENTITY_RUNTIME_SIGNING_SECRET_FILE",
            "IDENTITY_MAIL_V2_DATABASE_URL_FILE",
            "IDENTITY_MAIL_V2_SMTP_PASSWORD",
            "ASSETS_SOURCE_NATIVE_PYRAMID_CLEANUP_TOKEN_FILE",
            "ASSETS_IMAGES_NATIVE_STORAGE_TOKEN_FILE",
            "LABELING_RUNTIME_VIDEO_WRITER_DATABASE_URL_FILE",
        )
        publication_url = "http://notifications-publisher:8181"
        reader_url = "http://assets-source:8767"
        values = {
            **PRODUCTION,
            **dict.fromkeys(private_only, "owner-only"),
            "NOTIFICATION_PUBLICATION_URL": publication_url,
            "ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL": reader_url,
            "ASSETS_VIDEO_EXECUTOR": "split",
            "ASSETS_IMAGE_EXECUTOR": "split",
        }
        sources = {}
        for key in files:
            source = self.root / key.lower()
            source.write_text("fixture-only", encoding="utf-8")
            sources[key] = source
            values[key] = str(source)

        for lan in (False, True):
            with self.subTest(lan=lan):
                services = self.config(
                    production=values, lan=lan, profile="maintenance" if lan else None
                )
                for name, service in services.items():
                    env = service.get("environment", {})
                    for key, (target, owners) in files.items():
                        if name in owners:
                            self.assertEqual(env[key], target, (name, key))
                            self.assert_read_only_bind(service, target, sources[key])
                        else:
                            self.assertFalse(env.get(key), (name, key))
                            self.assertNotIn(
                                target,
                                [v.get("target") for v in service.get("volumes", [])],
                            )
                    if name == "api" or (
                        name.startswith("celery-") and name != JOBS_RECOVERY
                    ):
                        self.assertEqual(env["ASSETS_VIDEO_EXECUTOR"], "split")
                        self.assertEqual(env["ASSETS_IMAGE_EXECUTOR"], "split")
                    self.assertFalse(any(env.get(key) for key in private_only), name)
                    if name in {
                        "api",
                        "celery-worker",
                        "celery-worker-export",
                        "celery-beat",
                    }:
                        self.assertEqual(
                            env["NOTIFICATION_PUBLICATION_URL"], publication_url
                        )
                    else:
                        self.assertFalse(env.get("NOTIFICATION_PUBLICATION_URL"))
                    if name in {"api", "celery-worker"}:
                        self.assertEqual(
                            env["ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL"], reader_url
                        )
                    else:
                        self.assertFalse(
                            env.get("ASSETS_MEDIA_NATIVE_IMAGE_STORAGE_URL")
                        )
                self.assertNotIn("fixture-only", json.dumps(services))


if __name__ == "__main__":
    unittest.main()
