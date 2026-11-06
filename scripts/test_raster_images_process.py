"""Restricted Images executor environment and exact shutdown outcomes."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from linux_pidfd import open_pidfd
from raster_images_process import RasterImagesProcess, _UNSETTLED, image_environment
import run_raster_fault_e2e as raster_runner
from storage_s3_contract import ContractError


class RasterImagesEnvironmentTests(unittest.TestCase):
    def test_worker_receives_only_dedicated_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            nonce = "a" * 32
            credentials = {
                "database_role": "aap_raster_images_" + nonce,
                "database_password": "image-db-secret",
                "access_key": "image-access",
                "secret_key": "image-secret",
            }
            path = private / "images.json"
            path.write_text(json.dumps(credentials))
            path.chmod(0o600)
            buckets = {
                "MINIO_BUCKET": "legacy",
                "MINIO_DATASETS_BUCKET": "datasets",
                "MINIO_MEDIA_CACHE_BUCKET": "cache",
                "MINIO_INPUTS_BUCKET": "inputs",
            }
            owner = SimpleNamespace(private=private, data={
                "nonce": nonce,
                "raster": {
                    "resources": {"buckets": buckets},
                    "endpoint": "127.0.0.1:9000",
                    "images": {"database_role": credentials["database_role"],
                               "s3_user": credentials["access_key"]},
                },
            })
            parent = {
                "DATABASE_URL": "postgresql+asyncpg://ordinary:ordinary-secret@localhost/owned",
                "MINIO_ROOT_USER": "root",
                "MINIO_ROOT_PASSWORD": "root-secret",
                "AWS_SECRET_ACCESS_KEY": "aws-secret",
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT": "http://127.0.0.1:9001",
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET": "fixture-originals",
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY": "reader",
                "AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY": "reader-secret",
            }
            with patch.object(Path, "is_file", return_value=True):
                result = image_environment(owner, parent)
            self.assertNotIn("MINIO_ROOT_USER", result)
            self.assertNotIn("MINIO_ROOT_PASSWORD", result)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", result)
            self.assertNotIn("ordinary-secret", str(result))
            self.assertEqual(result["ASSETS_MEDIA_S3_ACCESS_KEY"], "image-access")
            self.assertEqual(result["ASSETS_MEDIA_S3_SECRET_KEY"], "image-secret")
            self.assertIn("aap_raster_images_", result["ASSETS_IMAGES_DATABASE_URL"])
            self.assertEqual(result["AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY"], "reader")

    def test_credentials_file_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "images.json"
            path.write_text("{}")
            path.chmod(0o644)
            owner = SimpleNamespace(private=Path(directory), data={})
            with self.assertRaisesRegex(ContractError, "raster_images_credentials_unsafe"):
                image_environment(owner, {})


class RasterImagesDestroyTests(unittest.TestCase):
    def test_guarded_destroy_failure_retains_original_role(self):
        with tempfile.TemporaryDirectory() as directory:
            role = Path(directory) / "original-role"
            role.write_text("oid")
            events = []

            class Backend:
                @contextmanager
                def fixture_authority_fence(self):
                    events.append("locked")
                    try:
                        yield
                    finally:
                        events.append("unlocked")

                def _destroy_owned(self, confirmation):
                    assert confirmation == "owned"
                    assert role.read_text() == "oid"
                    raise RuntimeError("fixture store refused destruction")

            owner = SimpleNamespace(
                data={"raster": {"images": {}}},
                save=lambda: events.append("journal"),
            )

            async def drop_role(*_args):
                role.unlink()

            with patch.object(raster_runner, "image_role", drop_role):
                with self.assertRaisesRegex(RuntimeError, "refused destruction"):
                    raster_runner.destroy_with_images_role(Backend(), owner, "owned")
            self.assertEqual(role.read_text(), "oid")
            self.assertEqual(events, ["locked", "journal", "unlocked"])
            self.assertEqual(owner.data["raster"]["images"]["state"], "guarded_destroy_intent")

    def test_original_role_is_removed_only_after_destroy_under_fence(self):
        with tempfile.TemporaryDirectory() as directory:
            role = Path(directory) / "original-role"
            role.write_text("oid")
            events = []

            class Backend:
                @contextmanager
                def fixture_authority_fence(self):
                    events.append("locked")
                    try:
                        yield
                    finally:
                        events.append("unlocked")

                def _destroy_owned(self, confirmation):
                    assert role.read_text() == "oid"
                    assert confirmation == "owned"
                    events.append("destroyed")

            owner = SimpleNamespace(
                data={"raster": {"images": {}}},
                save=lambda: None,
            )

            async def drop_role(*_args):
                assert events == ["locked", "destroyed"]
                role.unlink()
                events.append("role-removed")

            with patch.object(raster_runner, "image_role", drop_role):
                raster_runner.destroy_with_images_role(Backend(), owner, "owned")
            self.assertFalse(role.exists())
            self.assertEqual(events, ["locked", "destroyed", "role-removed", "unlocked"])


@unittest.skipUnless(
    sys.platform == "linux" and platform.machine() in {"x86_64", "aarch64"},
    "requires Linux pidfds",
)
class RasterImagesShutdownTests(unittest.TestCase):
    def process(self, program):
        settled_fd, writer = os.pipe()
        try:
            child = subprocess.Popen(
                [sys.executable, "-c", program],
                stdout=subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, "TEST_SETTLED_FD": str(writer)},
                pass_fds=(writer,),
            )
        finally:
            os.close(writer)
        self.addCleanup(self.cleanup, child)
        self.assertEqual(child.stdout.readline(), b"ready\n")
        records = []
        owner = RasterImagesProcess({}, root=Path.cwd(), journal=lambda *args: records.append(args))
        owner.child = child
        owner.descriptor = open_pidfd(child.pid)
        owner.settled_fd = settled_fd
        return owner, records

    @staticmethod
    def cleanup(child):
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
        while _UNSETTLED:
            owner = _UNSETTLED.pop()
            if owner.descriptor is not None:
                os.close(owner.descriptor)
            if owner.settled_fd is not None:
                os.close(owner.settled_fd)
            for descriptor in owner.tracked.values():
                os.close(descriptor)

    def test_requested_zero_exit_is_recorded(self):
        owner, records = self.process(
            "import os,signal,sys,time; "
            "fd=int(os.environ['TEST_SETTLED_FD']); "
            "signal.signal(signal.SIGTERM, lambda *_: (os.write(fd,b'settled\\n'),sys.exit(0))); "
            "print('ready',flush=True); time.sleep(30)"
        )
        owner.close()
        self.assertEqual(records, [("stopped", {
            "pid": owner.child.pid, "exit_code": 0, "shutdown_requested": True,
        })])

    def test_zero_exit_without_native_settlement_is_rejected(self):
        owner, records = self.process(
            "import signal,sys,time; "
            "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
            "print('ready',flush=True); time.sleep(30)"
        )
        with self.assertRaisesRegex(ContractError, "raster_images_settlement_unconfirmed"):
            owner.close()
        self.assertEqual(records, [])

    def test_unexpected_exit_cannot_be_reported_as_orderly(self):
        owner, records = self.process("import sys; print('ready',flush=True); sys.exit(7)")
        owner.child.wait()
        with self.assertRaises(ContractError):
            owner.close()
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
