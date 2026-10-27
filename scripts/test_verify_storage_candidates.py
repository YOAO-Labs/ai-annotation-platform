"""CLI and cleanup regressions without any configured application service."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from storage_s3_contract import ContractError
from verify_storage_candidates import (
    ROOT,
    Owner,
    candidate_args,
    expect_error,
    main,
    private_write,
    s3_config,
)


class RunnerTests(unittest.TestCase):
    def test_private_file_is_exclusive_and_mode_0600(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            private_write(path, b"secret")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                private_write(path, b"overwritten")
            self.assertEqual(path.read_bytes(), b"secret")

    def test_http_headers_are_case_insensitive(self):
        from storage_s3_contract import Budget
        from verify_storage_candidates import http_get

        response = Mock(status=200)
        response.headers = {"content-length": "3", "Content-Range": "bytes 0-2/3"}
        response.read.return_value = b"abc"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch(
            "verify_storage_candidates.urllib.request.urlopen", return_value=response
        ):
            status, headers, body = http_get("http://127.0.0.1:1/object", Budget())
        self.assertEqual(
            (status, headers["content-length"], headers["content-range"], body),
            (200, "3", "bytes 0-2/3", b"abc"),
        )

    def test_no_sdk_retry_and_finite_timeouts(self):
        config = s3_config()
        self.assertEqual(config.retries, {"total_max_attempts": 1})
        self.assertEqual((config.connect_timeout, config.read_timeout), (3, 15))
        self.assertEqual(config.s3["addressing_style"], "path")

    def test_creation_intent_precedes_docker_call_and_exact_id_precedes_start(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.json"
            owner = Owner(path, create=True)

            def create(args):
                data = json.loads(path.read_bytes())
                self.assertEqual(data["resources"][0]["state"], "creation_intent")
                self.assertIsNone(data["resources"][0]["id"])
                return "immutable-network-id"

            with patch("verify_storage_candidates.command", side_effect=create):
                item = owner.create("network")
            self.assertEqual(item["id"], "immutable-network-id")
            self.assertEqual(
                json.loads(path.read_bytes())["resources"][0]["id"], item["id"]
            )
            self.assertEqual(owner.data["checkout"], str(ROOT))

    def test_cleanup_refuses_foreign_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "owner.json", create=True)
            item = {
                "kind": "volume",
                "name": "exact",
                "id": "exact",
                "state": "created",
                "image": None,
            }
            owner.data["resources"].append(item)
            observed = {
                "Name": "exact",
                "Labels": {"aap.e07.owner": "other", "aap.e07.checkout": str(ROOT)},
            }
            with (
                patch.object(owner, "inspect", return_value=observed),
                patch("verify_storage_candidates.command") as command,
            ):
                self.assertFalse(owner.cleanup())
            command.assert_not_called()
            self.assertTrue(owner.private.exists())
            self.assertEqual(owner.data["cleanup_errors"], ["owner_label"])

    def test_cleanup_interruption_preserves_manifest_and_can_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.json"
            owner = Owner(path, create=True)
            item = {
                "kind": "container",
                "name": "exact",
                "id": "exact-id",
                "image": "pinned",
                "state": "created",
            }
            owner.data["resources"].append(item)
            observed = {
                "Id": "exact-id",
                "Config": {
                    "Image": "pinned",
                    "Labels": {
                        "aap.e07.owner": owner.data["nonce"],
                        "aap.e07.checkout": str(ROOT),
                    },
                },
            }
            with (
                patch.object(owner, "inspect", return_value=observed),
                patch("verify_storage_candidates.command", side_effect=TimeoutError()),
            ):
                self.assertFalse(owner.cleanup())
            self.assertFalse(json.loads(path.read_bytes())["cleaned"])
            with patch.object(owner, "inspect", return_value=None):
                self.assertTrue(owner.cleanup())
            self.assertFalse(owner.private.exists())
            self.assertEqual(owner.data["resources"][0]["state"], "absent")

    def test_absent_docker_response_is_case_insensitive(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "owner.json", create=True)
            response = Mock(returncode=1, stderr=b"error: no such object: exact")
            with patch(
                "verify_storage_candidates.subprocess.run", return_value=response
            ):
                self.assertIsNone(
                    owner.inspect({"kind": "container", "id": "exact", "name": "exact"})
                )

    def test_cleanup_foreign_network_users_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "owner.json", create=True)
            item = {
                "kind": "network",
                "id": "exact",
                "name": "exact",
                "state": "created",
            }
            owner.data["resources"].append(item)
            observed = {
                "Id": "exact",
                "Containers": {"foreign": {}},
                "Labels": {
                    "aap.e07.owner": owner.data["nonce"],
                    "aap.e07.checkout": str(ROOT),
                },
            }
            with (
                patch.object(owner, "inspect", return_value=observed),
                patch("verify_storage_candidates.command") as command,
            ):
                self.assertFalse(owner.cleanup())
            command.assert_not_called()
            self.assertEqual(owner.data["cleanup_errors"], ["network_in_use"])

    def test_atomic_manifest_write_recovers_task_owned_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "owner.json", create=True)
            temporary = Path(owner.data["atomic_temporary"])
            private_write(temporary, b"partial")
            owner.save()
            self.assertFalse(temporary.exists())
            self.assertEqual(
                json.loads(owner.path.read_bytes())["nonce"], owner.data["nonce"]
            )

    def test_atomic_manifest_write_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Owner(Path(directory) / "owner.json", create=True)
            outside = Path(directory) / "unrelated"
            outside.write_text("preserve")
            Path(owner.data["atomic_temporary"]).symlink_to(outside)
            with self.assertRaises(ContractError):
                owner.save()
            self.assertEqual(outside.read_text(), "preserve")

    def test_no_application_or_existing_port_configuration(self):
        args, cmd = candidate_args(
            "seaweedfs",
            Path("/private"),
            Path("/private/owned.env"),
            {"id": "owned-volume"},
            {"id": "owned-network"},
        )
        self.assertRegex(
            args[args.index("--publish") + 1], r"^127\.0\.0\.1:[0-9]+:9000$"
        )
        self.assertIn("-s3.config=/data/poc-s3.json", cmd)
        self.assertNotIn("3100", " ".join(args + cmd))
        self.assertNotIn("8100", " ".join(args + cmd))

    def test_expected_error_rejects_wrong_status_or_success_and_closes_body(self):
        from botocore.exceptions import ClientError

        error = ClientError(
            {
                "Error": {"Code": "AccessDenied", "Message": "secret"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "GetObject",
        )
        action = Mock(side_effect=error)
        self.assertEqual(
            expect_error(action, statuses={403})["error_code"], "AccessDenied"
        )
        with self.assertRaises(ContractError):
            expect_error(action, statuses={404})
        body = io.BytesIO(b"abc")
        with self.assertRaises(ContractError):
            expect_error(lambda: {"Body": body}, statuses={404})
        self.assertTrue(body.closed)

    def test_absent_resources_do_not_bypass_nonce_validation_before_directory_cleanup(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.json"
            owner = Owner(path, create=True)
            foreign = Path(directory) / "private-invalid"
            foreign.mkdir()
            sentinel = foreign / "preserve"
            sentinel.write_text("unrelated")
            owner.data.update(
                nonce="invalid", private_directory=str(foreign), resources=[]
            )
            owner.save()
            with (
                self.assertRaisesRegex(ContractError, "owner_nonce"),
                patch("verify_storage_candidates.shutil.rmtree") as remove,
            ):
                Owner(path).cleanup()
            remove.assert_not_called()
            self.assertEqual(sentinel.read_text(), "unrelated")

    def test_cleanup_manifest_bound_to_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "owner.json"
            owner = Owner(path, create=True)
            owner.data["checkout"] = "/other"
            owner.save()
            with self.assertRaises(ContractError):
                Owner(path)

    def test_recovery_rejects_empty_or_unversioned_confirmation(self):
        from verify_storage_candidates import verify_saved

        for saved in ([], [("bucket", "key", None, b"confirmed", {})]):
            with self.assertRaises(ContractError):
                verify_saved(Mock(), Mock(), saved)

    def test_cli_cleanup_failure_exit_code(self):
        owner = Mock()
        owner.cleanup.return_value = False
        owner.data = {"resources": []}
        with (
            patch("verify_storage_candidates.Owner", return_value=owner),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            self.assertEqual(main(["cleanup", "--manifest", "/private/owner.json"]), 2)

    def test_actual_signer_preserves_raw_keys_and_ignores_deployment_settings(self):
        import urllib.parse
        from storage_s3_contract import Budget
        from verify_storage_candidates import app_storage

        buckets = ["owned-poc-" + str(index) for index in range(9)]
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "postgresql://business:secret@unreachable/business",
                "MINIO_ENDPOINT": "business.invalid:9000",
            },
        ):
            service, settings = app_storage(
                "http://127.0.0.1:1",
                ("owned-local-key", "owned-local-secret"),
                buckets,
                Budget(),
            )
        self.assertEqual(
            settings.database_url, "postgresql+asyncpg://poc:poc@127.0.0.1:1/poc"
        )
        for key in (
            "raw/../literal",
            "raw//double",
            "raw/%2F",
            "raw/%2f",
            "中文/%25+字",
        ):
            url = service.generate_download_url(key, align=False, audience="internal")
            self.assertEqual(
                urllib.parse.unquote(urllib.parse.urlsplit(url).path),
                "/" + buckets[0] + "/" + key,
            )
        service.client.close()

    def test_module_import_does_not_load_application_config(self):
        # A fresh Python process importing the CLI must not initialize settings.
        import subprocess
        import sys

        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import verify_storage_candidates,sys; assert 'app.config' not in sys.modules",
            ],
            cwd=ROOT / "scripts",
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
