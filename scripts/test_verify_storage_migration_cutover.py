"""Pure fixture-contract checks; no Docker, database, Redis or HTTP resources."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode
import uuid

import verify_storage_migration_cutover as live
import verify_storage_migration_finalization as base
from storage_s3_contract import ContractError


A, B, ACTOR, GROUP = (str(uuid.uuid4()) for _ in range(4))


def bindings(location=A, generation=1):
    return {
        purpose: {
            "location_id": location,
            "bucket": "source-" + purpose if location == A else "target-bucket",
            "generation": generation,
        }
        for purpose in live.PURPOSES
    }


def put_url(*, now=None, expires=900):
    now = now or datetime.now(timezone.utc)
    return "http://172.28.0.2:80/staging?" + urlencode(
        {
            "X-Amz-Date": now.strftime("%Y%m%dT%H%M%SZ"),
            "X-Amz-Expires": expires,
            "X-Amz-Signature": "private-signature",
        }
    )


class VectorsTests(unittest.TestCase):
    def test_forward_and_rollback_preserve_all_nine_explicit_generations(self):
        before = bindings()
        saved = deepcopy(before)
        group = {
            "state": "active",
            "before_bindings": before,
            "after_bindings": bindings(B, 2),
        }
        live.check_forward(group, before, B, "target-bucket")
        self.assertEqual(live.restored_bindings(before), bindings(A, 3))
        self.assertEqual(before, saved)

    def test_partial_scope_boolean_generation_or_wrong_target_are_rejected(self):
        for change in ("missing", "boolean", "target"):
            with self.subTest(change=change):
                before, after = bindings(), bindings(B, 2)
                if change == "missing":
                    before.pop("training")
                elif change == "boolean":
                    before["annotations"]["generation"] = True
                else:
                    after["datasets"]["location_id"] = A
                with self.assertRaises(ContractError):
                    live.check_forward(
                        {
                            "state": "active",
                            "before_bindings": before,
                            "after_bindings": after,
                        },
                        before,
                        B,
                        "target-bucket",
                    )


class TransportTests(unittest.TestCase):
    def test_staging_put_preserves_url_with_the_sdk_default_port_host(self):
        from botocore.auth import _host_from_url

        url = put_url()
        with patch.object(live, "request", return_value=(200, {}, b"")) as request:
            live.put_target_staging(url, b"payload")
        request.assert_called_once_with(
            url,
            method="PUT",
            body=b"payload",
            headers={"Content-Type": "image/png", "Host": _host_from_url(url)},
        )

    def test_only_owned_api_capabilities_are_opened(self):
        http = SimpleNamespace(endpoint="http://127.0.0.1:8109")
        for path in (
            "/api/v1/media-cache/thumbnail/id/file?token=private",
            "http://127.0.0.1:8109/api/v1/files/task",
        ):
            self.assertTrue(live.application_path(http, path).startswith("/api/v1/"))
        for path in (
            "https://other.test/api/v1/file",
            "//other.test/api/v1/file",
            "/outside/file",
            "/api/v1/file#fragment",
        ):
            with self.subTest(path=path), self.assertRaises(ContractError):
                live.application_path(http, path)

    def test_old_put_must_still_be_valid_when_physical_fence_is_tested(self):
        now = datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
        live.signed_put_live(put_url(now=now), now=now + timedelta(seconds=899))
        for url, clock in (
            (put_url(now=now), now + timedelta(seconds=900)),
            (put_url(now=now), now - timedelta(seconds=1)),
            (put_url(now=now, expires=901), now),
            (put_url(now=now) + "&X-Amz-Date=duplicate", now),
        ):
            with self.subTest(url=url), self.assertRaises(ContractError):
                live.signed_put_live(url, now=clock)

    def test_authenticated_original_rechecks_identity_and_rejects_anonymous_read(self):
        http = SimpleNamespace(endpoint="http://127.0.0.1:8109", token="private")
        seen = []

        def call(method, path, *, binary=False, expected=200):
            seen.append((http.token, method, path, expected))
            if path.endswith("original-identity"):
                return {
                    "content_id": "original",
                    "sha256": live.sha256(b"image"),
                    "size_bytes": 5,
                    "download_path": "/api/v1/original/file",
                }
            return b"image" if binary else {"detail": "unauthenticated"}

        http.call = call
        live.Cutover().read_original(http, "task", live.sha256(b"image"), 5, "original")
        self.assertEqual(seen[-1], (None, "GET", "/api/v1/original/file", 401))
        self.assertEqual(http.token, "private")


class FakeOwner:
    def __init__(self, directory):
        self.private = Path(directory)
        self.data = {"nonce": "a" * 32}
        self.calls, self.resources = [], {}
        self.role = "fixture"

    def resource(self, kind, role, *, args=()):
        identifier = f"{len(self.resources) + 1:064x}"
        result = {"id": identifier, "kind": kind, "role": role}
        self.resources[identifier] = result
        self.calls.append(("resource", kind, role, args))
        return result

    def service(self, role, image, *, args=(), cmd=()):
        self.role = role
        item = self.container(image, args=args, cmd=cmd)
        self.start(item)
        return item

    def container(self, image, *, args=(), cmd=()):
        item = self.resource("container", self.role)
        self.calls.append(("container", self.role, args, cmd))
        return item

    def action(self, item, *args):
        self.calls.append(("action", item["id"], args))

    def start(self, item):
        self.calls.append(("start", item["id"]))

    def verified(self, item):
        if item["kind"] == "network":
            return {"IPAM": {"Config": [{"Subnet": "172.25.0.0/24"}]}}
        return {
            "NetworkSettings": {"Networks": {"bridge": {"IPAddress": "172.27.0.2"}}}
        }

    def save(self):
        pass


class ProvisionTests(unittest.TestCase):
    def test_media_worker_uses_cached_image_current_source_and_only_application_env(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            owner = Mock()
            owner.private = Path(temporary)
            owner.data = {"nonce": "a" * 32}
            owner.service.return_value = {"id": "b" * 64}
            owner.verified.return_value = {"State": {"Running": True}}
            fixture = live.Cutover()
            settings = SimpleNamespace(
                model_fields={"database_url": None, "redis_url": None}
            )
            with (
                patch.dict(
                    "sys.modules", {"app.config": SimpleNamespace(Settings=settings)}
                ),
                patch.object(
                    live,
                    "command",
                    side_effect=[
                        json.dumps([{"Id": fixture.media_image}]),
                        json.dumps({"vips": [8, 14, 1]}),
                    ],
                ),
                patch.object(
                    live.subprocess, "run", return_value=SimpleNamespace(returncode=0)
                ),
            ):
                fixture.start_media_worker(
                    owner,
                    {
                        "DATABASE_URL": "owned-db",
                        "REDIS_URL": "owned-redis",
                        "AAP_WORKTREE_MODE": "test",
                        "AAP_SOURCE_FENCE_SECRET_KEY": "native-private",
                        "AAP_TARGET_FENCE_SECRET_KEY": "native-private-b",
                        "DOCKER_HOST": "unowned",
                        "PATH": "/host/python",
                    },
                )
            payload = (owner.private / "media-worker.env").read_text()
            self.assertIn("DATABASE_URL=owned-db\n", payload)
            self.assertIn("AAP_WORKTREE_MODE=test\n", payload)
            self.assertNotIn("native-private", payload)
            self.assertNotIn("DOCKER_HOST", payload)
            self.assertNotIn("/host/python", payload)
            self.assertIn(
                "PYTHONPATH=/app:/_shared/task_contract/src:/_shared/protocol_v2/src:/_shared/training_contract\n",
                payload,
            )
            self.assertEqual(fixture.media_vips, [8, 14, 1])
            args = owner.service.call_args.kwargs["args"]
            self.assertIn("--pull=never", args)
            self.assertIn(str(base.ROOT / "apps/api") + ":/app:ro", args)
            self.assertIn(str(base.ROOT / "apps/_shared") + ":/_shared:ro", args)
            self.assertIn("/opt/venv/bin/python", args)
            self.assertIn("--queues=media", owner.service.call_args.kwargs["cmd"])

    def test_gateway_config_keeps_required_mode_under_private_process_umask(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            previous = os.umask(0o077)
            try:
                base.replace_config(directory / "nginx.conf", b"owned config")
            finally:
                os.umask(previous)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "nginx.conf").stat().st_mode & 0o777, 0o644)
            self.assertEqual((directory / "nginx.conf").read_bytes(), b"owned config")
            self.assertEqual(
                [path.name for path in directory.iterdir()], ["nginx.conf"]
            )

    def test_b_native_config_has_independent_networks_and_no_minio_ingress(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner, fixture = FakeOwner(temporary), live.Cutover()
            env_file = Path(temporary) / "target.env"
            env_file.write_text("private credentials")
            env_file.chmod(0o600)
            adapter = Mock()
            adapter.application_endpoint.return_value = "http://172.28.0.2:80"
            with (
                patch(
                    "storage_source_fence_docker.DockerAdapter", return_value=adapter
                ) as constructor,
                patch.object(live, "free_port", return_value=19002),
                patch.object(live, "host_port", return_value="19002"),
            ):
                endpoint, address = fixture.provision_target(
                    owner, ("b-access", "b-secret"), env_file
                )
            self.assertEqual(
                (endpoint, address), ("http://172.28.0.2:80", "172.28.0.2")
            )
            config = json.loads(fixture.target_config.read_bytes())
            self.assertEqual(config["public_endpoint"], "http://127.0.0.1:19002")
            self.assertNotEqual(
                config["network_id"], config["gateway_frontend_network_id"]
            )
            self.assertEqual(set(config["purpose_buckets"]), live.PURPOSES)
            self.assertEqual(
                set(config["purpose_buckets"].values()), {fixture.target_bucket}
            )
            self.assertEqual(
                fixture.target_state,
                Path(config["gateway_config_directory"]).parent / "fence-state.json",
            )
            self.assertEqual(fixture.target_config.stat().st_mode & 0o077, 0)
            self.assertEqual(
                constructor.call_args.kwargs, {"credential_prefix": "AAP_TARGET_FENCE"}
            )
            source = next(
                call for call in owner.calls if call[:2] == ("container", "source")
            )
            gateway = next(
                call for call in owner.calls if call[:2] == ("container", "gateway")
            )
            self.assertNotIn("--publish", source[2])
            self.assertIn("127.0.0.1:19002:80", gateway[2])
            self.assertIn("net.ipv4.ip_forward=0", gateway[2])
            self.assertTrue(
                any(value.endswith(":/etc/nginx:ro") for value in gateway[2])
            )

    def test_optional_factory_preserves_the_existing_provision_return_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            owner = FakeOwner(temporary)
            factory = Mock(return_value=("http://172.28.0.2:80", "172.28.0.2"))
            with (
                patch.object(base, "command", return_value='[{"owned":true}]'),
                patch.object(base, "free_port", return_value=19003),
                patch.object(base, "host_port", return_value="19003"),
            ):
                result = base.provision(owner, target_factory=factory)
            self.assertEqual(len(result), 7)
            self.assertEqual(result[2], "http://172.28.0.2:80")
            self.assertEqual(result[4], "172.28.0.2")
            self.assertEqual(
                factory.call_args.args,
                (owner, result[3], Path(temporary) / "target.env"),
            )
            self.assertFalse(
                any(call[:2] == ("container", "target") for call in owner.calls)
            )


class OperatorTests(unittest.TestCase):
    def test_cli_keeps_credential_namespaces_separate_without_global_mutation(self):
        owner = SimpleNamespace(data={}, save=Mock())
        environment = {
            "AAP_SOURCE_FENCE_ACCESS_KEY": "a",
            "AAP_TARGET_FENCE_ACCESS_KEY": "b",
        }
        old = dict(os.environ)
        result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "schema": "aap.storage-migration-cutover-cli/v1",
                    "item": {"state": "active"},
                }
            ).encode(),
            stderr=b"",
        )
        with patch.object(live.subprocess, "run", return_value=result) as run:
            self.assertEqual(
                live.cutover_operator(
                    owner,
                    environment,
                    "activate",
                    ACTOR,
                    GROUP,
                    review_file=Path("private review.json"),
                ),
                {"state": "active"},
            )
        self.assertEqual(run.call_args.kwargs["env"], environment)
        self.assertEqual(
            run.call_args.args[0][-2:], ["--review-file", "private review.json"]
        )
        self.assertEqual(dict(os.environ), old)

    def test_operator_failure_keeps_only_bounded_safe_diagnostics(self):
        owner = SimpleNamespace(data={}, save=Mock())
        for code, expected in (
            ("cutover_changed", "cutover_changed"),
            ("https://secret.test/token", None),
        ):
            with self.subTest(code=code):
                result = SimpleNamespace(
                    returncode=2,
                    stdout=b"private output",
                    stderr=json.dumps({"code": code}).encode(),
                )
                with (
                    patch.object(live.subprocess, "run", return_value=result),
                    self.assertRaises(ContractError),
                ):
                    live.cutover_operator(owner, {}, "activate", ACTOR, GROUP)
                self.assertEqual(owner.data["operator_failure"].get("code"), expected)
                self.assertNotIn("private", json.dumps(owner.data))
                self.assertNotIn("secret", json.dumps(owner.data))


if __name__ == "__main__":
    unittest.main()
