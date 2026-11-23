"""Pure worktree topology tests; never import API database fixtures."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


class IdentityTests(unittest.TestCase):
    def test_identity_is_persistent_checkout_bound_and_modes_are_disjoint(self):
        self.assertIsNotNone(
            importlib.util.find_spec("worktree_env"),
            "The launcher needs an owned, persistent environment topology",
        )
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = load_identity(root, create=True)
            self.assertEqual(first, load_identity(root))
            dev = topology(root, first, "dev")
            test = topology(root, first, "test")
            self.assertNotEqual(dev["database"], test["database"])
            self.assertNotEqual(dev["redis_name"], test["redis_name"])
            self.assertTrue(test["database"].endswith("_test"))
            self.assertTrue(
                set(dev["buckets"].values()).isdisjoint(test["buckets"].values())
            )
            self.assertEqual(len(dev["buckets"]), 10)
            self.assertEqual(
                dev["buckets"]["MINIO_INPUTS_BUCKET"],
                f"yoao-wt-{first['id']}-dev-accepted-inputs",
            )
            document = json.loads((root / ".worktree/identity.json").read_text())
            self.assertEqual(document["root"], str(root.resolve()))
            self.assertNotIn("password", json.dumps(document).lower())

    def test_rejects_copied_identity_and_symlinked_state(self):
        from worktree_env import load_identity

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first", root / "second"
            first.mkdir()
            second.mkdir()
            load_identity(first, create=True)
            (second / ".worktree").symlink_to(first / ".worktree")
            with self.assertRaisesRegex(ValueError, "symlink|符号链接"):
                load_identity(second, create=True)
            (second / ".worktree").unlink()
            (second / ".worktree").mkdir()
            (second / ".worktree/identity.json").write_bytes(
                (first / ".worktree/identity.json").read_bytes()
            )
            with self.assertRaisesRegex(ValueError, "checkout|工作树"):
                load_identity(second)

    def test_rejects_malformed_identity_and_modes(self):
        from worktree_env import load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity = load_identity(root, create=True)
            path = root / ".worktree/identity.json"
            path.write_text(json.dumps({**identity, "id": "../../annotation"}))
            with self.assertRaises(ValueError):
                load_identity(root)
            with self.assertRaises(ValueError):
                topology(root, identity, "../../shared")

    def test_refuses_retired_identity_or_orphaned_mode_without_minting_a_replacement(
        self,
    ):
        from worktree_env import WorktreeError, load_identity

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mode = root / ".worktree" / "test"
            mode.mkdir(parents=True)
            path = mode.parent / "identity.json"
            old = json.dumps({"schema": 1, "id": "ab" * 8, "root": str(root)})
            path.write_text(old)
            with self.assertRaises(WorktreeError):
                load_identity(root, create=True)
            self.assertEqual(path.read_text(), old)
            path.unlink()
            with self.assertRaises(WorktreeError):
                load_identity(root, create=True)
            self.assertFalse(path.exists())

    def test_concurrent_initializers_publish_one_complete_identity(self):
        from concurrent.futures import ThreadPoolExecutor
        from worktree_env import load_identity

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with ThreadPoolExecutor(max_workers=8) as pool:
                identities = list(
                    pool.map(lambda _: load_identity(root, create=True), range(32))
                )
            self.assertTrue(all(identity == identities[0] for identity in identities))


class EnvironmentTests(unittest.TestCase):
    def test_application_process_cannot_inherit_private_worker_dsns(self):
        from worktree_env import application_environment

        private = {
            "inference_gpu_database_url": "gpu-secret",
            "INFERENCE_GPU_DATABASE_URL_FILE": "/gpu-secret",
            "inference_backend_endpoint_setup_database_url_file": "/endpoint-secret",
            "INFERENCE_BACKEND_STATS_DATABASE_URL": "stats-secret",
            "aap_module_data_inference_gpu_url": "verifier-secret",
            "AAP_MODULE_DATA_INFERENCE_HEALTH_DATA_URL_FILE": "/verifier-secret",
            "inference_health_data_database_url": "health-secret",
            "jobs_dispatch_database_url": "private-jobs-claim",
            "JOBS_DISPATCH_DATABASE_URL_FILE": "/jobs-claim",
        }
        environment = application_environment(
            {**private, "INFERENCE_BACKEND_CONTROL_URL": "http://fixture-owner"}
        )
        for key in private:
            self.assertNotIn(key, environment)
        self.assertEqual(
            environment["INFERENCE_BACKEND_CONTROL_URL"], "http://fixture-owner"
        )

    def test_application_clears_insights_refresh_selectors_case_insensitively(self):
        from worktree_env import application_environment

        env = application_environment(
            {
                "insights_refresh_database_url": "postgresql://fixture-owner/postgres",
                "Insights_Refresh_Database_Url_File": "/tmp/fixture-refresh",
                "DATABASE_URL": "postgresql://fixture-runtime/annotation",
            }
        )
        self.assertEqual(env["INSIGHTS_REFRESH_DATABASE_URL"], "")
        self.assertEqual(env["INSIGHTS_REFRESH_DATABASE_URL_FILE"], "")
        self.assertNotIn("insights_refresh_database_url", env)
        self.assertNotIn("Insights_Refresh_Database_Url_File", env)
        self.assertEqual(env["DATABASE_URL"], "postgresql://fixture-runtime/annotation")

    def test_every_identity_settings_database_selector_is_overridden(self):
        import ast
        from worktree_env import isolated_environment, load_identity, topology

        source = Path(__file__).resolve().parents[1] / "apps/api/app/config.py"
        settings = next(
            node
            for node in ast.parse(source.read_text()).body
            if isinstance(node, ast.ClassDef) and node.name == "Settings"
        )
        selectors = {
            node.target.id.upper()
            for node in settings.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id.startswith(("identity_", "notification_publish_"))
            and node.target.id.endswith(("database_url", "database_url_file"))
        }
        self.assertGreater(len(selectors), 30)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            inherited = {
                key: "shared-owner-secret"
                for name in selectors
                for key in (name, name.lower())
            }
            env = isolated_environment(
                resources,
                code_root=root,
                database_url="postgresql+asyncpg://runtime:fixture@localhost:5432/db",
                migration_database_url="postgresql+asyncpg://owner:fixture@localhost:5432/db",
                minio_endpoint="localhost:9000",
                redis_port=6391,
                inherited=inherited,
            )
            for name in selectors:
                self.assertEqual(env.get(name), "", name)
                self.assertNotIn(name.lower(), env)
            self.assertNotIn("shared-owner-secret", env.values())

    def test_worktree_disables_shared_module_selectors_including_dotenv_only_keys(self):
        from worktree_env import isolated_environment, load_identity, topology

        selectors = {
            "WORKSPACE_TEMPLATE_QUERY_DATABASE_URL_FILE",
            "WORKSPACE_PROJECT_QUERY_URL",
            "LABELING_CLASS_QUERY_DATABASE_URL",
            "LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE",
            "JOBS_QUEUE_TOKEN_FILE",
            "JOBS_DISPATCH_RELAY_DATABASE_URL_FILE",
            "JOBS_DISPATCH_DATABASE_URL",
            "JOBS_PUBLICATION_DATABASE_URL_FILE",
            "INSIGHTS_REFRESH_DATABASE_URL",
            "INSIGHTS_REFRESH_DATABASE_URL_FILE",
            "IDENTITY_URL",
            "IDENTITY_RUNTIME_SIGNING_SECRET_FILE",
            "IDENTITY_EMBEDDED_SIGNING_SECRET_FILE",
            "IDENTITY_MAIL_V2_DATABASE_URL_FILE",
            "NOTIFICATION_SOCKET_URL",
            "NOTIFICATION_PUBLICATION_TOKEN_FILE",
            "NOTIFICATION_PUBLISH_DATABASE_URL",
            "NOTIFICATION_PUBLISH_REDIS_URL",
            "NOTIFICATION_PUBLISH_REDIS_URL_FILE",
            "NOTIFICATION_SUBSCRIBE_REDIS_URL",
            "NOTIFICATION_SUBSCRIBE_REDIS_URL_FILE",
            "ASSETS_CATALOG_URL",
            "ASSETS_SOURCE_TOKEN_FILE",
            "ASSETS_SOURCE_URL",
            "ASSETS_SOURCE_NETWORK",
            "ASSETS_SOURCE_SAVED_IO_SPOOL_DIR",
            "ASSETS_SOURCE_SFTP_KNOWN_HOSTS_FILE",
            "ASSETS_SOURCE_TOKEN",
            "ASSETS_SOURCE_DATABASE_URL",
            "ASSETS_SOURCE_DATABASE_URL_FILE",
            "ASSETS_SOURCE_SAVED_IO_DATABASE_URL",
            "ASSETS_SOURCE_SAVED_IO_DATABASE_URL_FILE",
            "ASSETS_SOURCE_SAVED_IO_READ_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_READ_TOKEN_FILE",
            "ASSETS_SOURCE_SAVED_IO_WRITE_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_WRITE_TOKEN_FILE",
            "ASSETS_SOURCE_SAVED_IO_CLEANUP_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_CLEANUP_TOKEN_FILE",
            "ASSETS_SOURCE_SAVED_IO_CUTOVER_TOKEN",
            "ASSETS_SOURCE_SAVED_IO_CUTOVER_TOKEN_FILE",
            "ASSETS_SOURCE_CONNECTIONS_TOKEN",
            "ASSETS_SOURCE_CONNECTIONS_TOKEN_FILE",
            "ASSETS_SOURCE_CLIENT_READ_TOKEN_FILE",
            "ASSETS_SOURCE_CLIENT_WRITE_TOKEN_FILE",
            "ASSETS_SOURCE_CLIENT_CLEANUP_TOKEN_FILE",
            "ASSETS_SOURCE_CLIENT_CONNECTIONS_TOKEN_FILE",
            "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY",
            "ASSETS_SOURCE_CONNECTOR_ENCRYPTION_KEY_FILE",
            "ASSETS_SOURCE_NATIVE_PYRAMID_READER_TOKEN_FILE",
            "ASSETS_SOURCE_IMPORT_VERIFY_TOKEN_FILE",
            "ASSETS_SOURCE_S3_ACCESS_KEY_FILE",
            "CONNECTOR_ENCRYPTION_KEY",
            "CONNECTOR_ENCRYPTION_KEY_FILE",
            "ASSETS_CELERY_VIDEO_DATABASE_URL",
            "ASSETS_CELERY_IMAGE_DATABASE_URL_FILE",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            env = isolated_environment(
                resources,
                code_root=root,
                database_url="postgresql+asyncpg://runtime:fixture@localhost:5432/db",
                migration_database_url="postgresql+asyncpg://owner:fixture@localhost:5432/db",
                minio_endpoint="localhost:9000",
                redis_port=6391,
                inherited={
                    "ASSETS_VIDEO_EXECUTOR": "split",
                    **dict.fromkeys(
                        (name.lower() for name in selectors), "shared-secret"
                    ),
                },
            )
        for name in selectors:
            self.assertEqual(env.get(name), "", name)
        self.assertNotIn("shared-secret", env.values())
        self.assertEqual(env["ASSETS_VIDEO_EXECUTOR"], "celery")
        self.assertEqual(env["ASSETS_IMAGE_EXECUTOR"], "celery")

    def test_overrides_all_mutable_targets_without_changing_shared_settings(self):
        import worktree_env

        self.assertTrue(hasattr(worktree_env, "isolated_environment"))
        from worktree_env import load_identity, topology, isolated_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            inherited = {
                "database_url": "must-not-win",
                "CELERY_BROKER_URL": "redis://shared:6379/0",
                "MINIO_BUCKET": "annotations",
                "ASSETS_MEDIA_INPUTS_BUCKET": "shared-inputs",
                "assets_mask_cleanup_url": "http://shared-cleanup.test",
                "ASSETS_MASK_CONTENT_URL": "http://shared-content.test",
                "LABELING_MASK_GC_URL": "http://shared-labeling.test",
                "LABELING_MASK_GC_TOKEN": "shared-test-token",
                "identity_url": "http://shared-identity.test",
                "IDENTITY_TOKEN": "shared-identity-token",
                "notification_socket_url": "ws://shared-notifications.test:8180",
                "notification_publication_url": "http://shared-publisher.test:8181",
                "notification_publication_token": "shared-publication-token",
                "notification_publication_token_file": "/shared/publication-token",
                "IDENTITY_AUTH_DATABASE_URL": "postgresql://shared/identity",
                "IDENTITY_AUTH_DATABASE_URL_FILE": "/shared/identity-dsn",
                "IDENTITY_KEY_DATABASE_URL": "postgresql://shared/key-commands",
                "IDENTITY_KEY_DATABASE_URL_FILE": "/shared/key-dsn",
                "IDENTITY_SESSION_DATABASE_URL": "postgresql://shared/sessions",
                "IDENTITY_SESSION_DATABASE_URL_FILE": "/shared/session-dsn",
                "IDENTITY_ACCOUNT_DATABASE_URL": "postgresql://shared/accounts",
                "IDENTITY_ACCOUNT_DATABASE_URL_FILE": "/shared/account-dsn",
                "IDENTITY_PRESENCE_DATABASE_URL": "postgresql://shared/presence",
                "IDENTITY_PRESENCE_DATABASE_URL_FILE": "/shared/presence-dsn",
                "IDENTITY_RECOVERY_DATABASE_URL": "postgresql://shared/recovery",
                "IDENTITY_RECOVERY_DATABASE_URL_FILE": "/shared/recovery-dsn",
                "IDENTITY_RECOVERY_MAIL_KEY": "shared-mail-key",
                "IDENTITY_RECOVERY_MAIL_KEY_FILE": "/shared/mail-key",
                "IDENTITY_MAIL_DATABASE_URL": "postgresql://shared/mail",
                "IDENTITY_MAIL_DATABASE_URL_FILE": "/shared/mail-dsn",
                "IDENTITY_MAIL_SETTINGS_DATABASE_URL": "postgresql://shared/mail-settings",
                "IDENTITY_MAIL_SETTINGS_DATABASE_URL_FILE": "/shared/mail-settings-dsn",
                "DUCKDB_PATH": "/shared/analytics.duckdb",
                "MINIO_PUBLIC_URL": "http://localhost:9000",
                "MINIO_PROXY_TARGET": "http://shared:9000",
                "PATH": "/bin",
            }
            env = isolated_environment(
                resources,
                code_root=root,
                database_url="postgresql+asyncpg://runtime:fixture@localhost:5432/annotation",
                migration_database_url="postgresql+asyncpg://owner:fixture@localhost:5432/annotation",
                minio_endpoint="localhost:9000",
                redis_port=6391,
                inherited=inherited,
            )
            self.assertNotIn("database_url", env)
            self.assertTrue(env["DATABASE_URL"].endswith("/" + resources["database"]))
            self.assertIn("runtime:", env["DATABASE_URL"])
            self.assertIn("owner:", env["MIGRATION_DATABASE_URL"])
            self.assertEqual(env["TEST_DATABASE_URL"], env["MIGRATION_DATABASE_URL"])
            self.assertEqual(env["REDIS_URL"], env["CELERY_BROKER_URL"])
            self.assertIn("CELERY_RESULT_BACKEND", env)
            self.assertEqual(env["REDIS_URL"], env["CELERY_RESULT_BACKEND"])
            self.assertEqual(env["REDIS_URL"], env["CELERY_BROKER_READ_URL"])
            self.assertEqual(env["REDIS_URL"], env["CELERY_BROKER_WRITE_URL"])
            self.assertEqual(env["MINIO_BUCKET"], resources["buckets"]["MINIO_BUCKET"])
            self.assertEqual(
                env["MINIO_INPUTS_BUCKET"],
                resources["buckets"]["MINIO_INPUTS_BUCKET"],
            )
            self.assertEqual(
                env["ASSETS_MEDIA_INPUTS_BUCKET"],
                resources["buckets"]["MINIO_INPUTS_BUCKET"],
            )
            self.assertTrue(env["DUCKDB_PATH"].startswith(resources["directory"]))
            self.assertEqual(env["ML_BACKEND_OBSERVE_URLS"], "[]")
            self.assertNotIn("assets_mask_cleanup_url", env)
            self.assertNotIn("identity_url", env)
            self.assertNotIn("notification_socket_url", env)
            for key in (
                "notification_publication_url",
                "notification_publication_token",
                "notification_publication_token_file",
            ):
                self.assertNotIn(key, env)
            for key in (
                "ASSETS_MASK_CONTENT_URL",
                "ASSETS_MASK_CONTENT_TOKEN",
                "ASSETS_MASK_CLEANUP_URL",
                "ASSETS_MASK_CLEANUP_TOKEN",
                "LABELING_MASK_GC_URL",
                "LABELING_MASK_GC_TOKEN",
                "IDENTITY_URL",
                "IDENTITY_TOKEN",
                "NOTIFICATION_SOCKET_URL",
                "NOTIFICATION_PUBLICATION_URL",
                "NOTIFICATION_PUBLICATION_TOKEN",
                "NOTIFICATION_PUBLICATION_TOKEN_FILE",
                "IDENTITY_AUTH_DATABASE_URL",
                "IDENTITY_AUTH_DATABASE_URL_FILE",
                "IDENTITY_KEY_DATABASE_URL",
                "IDENTITY_KEY_DATABASE_URL_FILE",
                "IDENTITY_SESSION_DATABASE_URL",
                "IDENTITY_SESSION_DATABASE_URL_FILE",
                "IDENTITY_ACCOUNT_DATABASE_URL",
                "IDENTITY_ACCOUNT_DATABASE_URL_FILE",
                "IDENTITY_PRESENCE_DATABASE_URL",
                "IDENTITY_PRESENCE_DATABASE_URL_FILE",
                "IDENTITY_RECOVERY_DATABASE_URL",
                "IDENTITY_RECOVERY_DATABASE_URL_FILE",
                "IDENTITY_RECOVERY_MAIL_KEY",
                "IDENTITY_RECOVERY_MAIL_KEY_FILE",
                "IDENTITY_MAIL_DATABASE_URL",
                "IDENTITY_MAIL_DATABASE_URL_FILE",
                "IDENTITY_MAIL_SETTINGS_DATABASE_URL",
                "IDENTITY_MAIL_SETTINGS_DATABASE_URL_FILE",
            ):
                self.assertEqual(env[key], "")
            self.assertEqual(env["PATH"], "/bin")
            self.assertEqual(inherited["MINIO_BUCKET"], "annotations")
            self.assertIn("PLAYWRIGHT_E2E_DATABASE_URL", env)
            self.assertEqual(
                env["PLAYWRIGHT_E2E_DATABASE_URL"], env["MIGRATION_DATABASE_URL"]
            )
            self.assertEqual(env["ML_BACKEND_ROUTER_MODE"], "off")
            self.assertEqual(env["VITE_WS_HOST"], "")
            self.assertEqual(env["MINIO_PUBLIC_URL"], "/minio")
            self.assertEqual(env["MINIO_PROXY_TARGET"], "http://localhost:9000")

    def test_resource_root_and_code_venv_are_independent(self):
        from worktree_env import isolated_environment, load_identity, topology

        with (
            tempfile.TemporaryDirectory() as resources_dir,
            tempfile.TemporaryDirectory() as code_dir,
        ):
            root = Path(resources_dir).resolve()
            code_root = Path(code_dir).resolve()
            resources = topology(root, load_identity(root, create=True), "test")
            env = isolated_environment(
                resources,
                code_root=code_root,
                database_url="postgresql+asyncpg://runtime:fixture@localhost/db",
                migration_database_url="postgresql+asyncpg://owner:fixture@localhost/db",
                minio_endpoint="localhost:9000",
                redis_port=6391,
                inherited={
                    "YOAO_WORKTREE_RESOURCE_ROOT": "/wrong-root",
                    "yoao_worktree_resource_root": "/also-wrong",
                    "UV_PROJECT_ENVIRONMENT": "/wrong-venv",
                },
            )
            self.assertEqual(env["YOAO_WORKTREE_RESOURCE_ROOT"], str(root))
            self.assertNotIn("yoao_worktree_resource_root", env)
            self.assertEqual(
                env["UV_PROJECT_ENVIRONMENT"], str(code_root / "apps/api/.venv")
            )
            self.assertTrue(env["TMPDIR"].startswith(resources["directory"]))

    def test_application_does_not_inherit_inference_owner_dsns(self):
        from worktree_env import application_environment, frontend_environment

        environment = {
            "INFERENCE_ROUTING_DATABASE_URL": "private-routing-dsn",
            "inference_routing_database_url_file": "/private/routing",
            "INFERENCE_HEALTH_OBSERVATION_DATABASE_URL": "private-health-dsn",
            "inference_health_observation_database_url_file": "/private/health",
            "aap_module_data_inference_routing_url": "private-routing-dsn",
            "AAP_MODULE_DATA_INFERENCE_HEALTH_OBSERVATION_URL": "private-health-dsn",
            "INFERENCE_ROUTING_URL": "http://127.0.0.1:8123",
            "INFERENCE_ROUTING_TOKEN": "caller-token",
        }
        for child in (
            application_environment(environment),
            frontend_environment(environment),
        ):
            for key in environment:
                if "_DATABASE_URL" in key.upper() or key.upper().startswith(
                    "AAP_MODULE_DATA_INFERENCE_"
                ):
                    self.assertNotIn(key, child)
        application = application_environment(environment)
        self.assertEqual(
            application["INFERENCE_ROUTING_URL"], environment["INFERENCE_ROUTING_URL"]
        )
        self.assertEqual(
            application["INFERENCE_ROUTING_TOKEN"],
            environment["INFERENCE_ROUTING_TOKEN"],
        )

    def test_e2e_does_not_select_inherited_inference_owners(self):
        from worktree_env import isolated_environment, load_identity, topology

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "e2e")
            environment = isolated_environment(
                resources,
                code_root=root,
                database_url="postgresql+asyncpg://runtime:fixture@localhost/db",
                migration_database_url="postgresql+asyncpg://owner:fixture@localhost/db",
                minio_endpoint="localhost:9000",
                redis_port=6391,
                inherited={
                    "inference_routing_url": "http://shared",
                    "INFERENCE_ROUTING_DATABASE_URL": "postgresql://shared",
                    "inference_health_observation_token_file": "/shared/token",
                    "aap_module_data_inference_routing_url": "postgresql://shared",
                },
            )
            self.assertNotIn("inference_routing_url", environment)
            self.assertEqual(environment["INFERENCE_ROUTING_URL"], "")
            self.assertEqual(environment["INFERENCE_ROUTING_DATABASE_URL"], "")
            self.assertNotIn("aap_module_data_inference_routing_url", environment)
            self.assertEqual(environment["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"], "")
            self.assertEqual(environment["INFERENCE_HEALTH_OBSERVATION_TOKEN_FILE"], "")

    def test_legacy_manifest_cannot_launch_with_a_fallback_inputs_bucket(self):
        from worktree_env import load_identity, topology, isolated_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "test")
            resources["buckets"].pop("MINIO_INPUTS_BUCKET")
            with self.assertRaisesRegex(ValueError, "显式运行 init"):
                isolated_environment(
                    resources,
                    code_root=root,
                    database_url="postgresql+asyncpg://runtime:fixture@localhost:5432/annotation",
                    migration_database_url="postgresql+asyncpg://owner:fixture@localhost:5432/annotation",
                    minio_endpoint="localhost:9000",
                    redis_port=6391,
                    inherited={"MINIO_INPUTS_BUCKET": "shared-inputs"},
                )

    def test_refuses_remote_or_mismatched_infrastructure_before_provisioning(self):
        import worktree_env

        self.assertTrue(hasattr(worktree_env, "isolated_environment"))
        from worktree_env import load_identity, topology, isolated_environment

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resources = topology(root, load_identity(root, create=True), "dev")
            local = "postgresql+asyncpg://owner:fixture@localhost:5432/annotation"
            for overrides in (
                {
                    "migration_database_url": local.replace(
                        "localhost", "production.example"
                    )
                },
                {"migration_database_url": local.replace("5432", "5433")},
                {"database_url": local + "?host=production.example"},
                {"minio_endpoint": "objects.example:9000"},
            ):
                arguments = dict(
                    code_root=root,
                    database_url=local,
                    migration_database_url=local,
                    minio_endpoint="localhost:9000",
                    redis_port=6391,
                    inherited={},
                )
                arguments.update(overrides)
                with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                    isolated_environment(resources, **arguments)


if __name__ == "__main__":
    unittest.main()
