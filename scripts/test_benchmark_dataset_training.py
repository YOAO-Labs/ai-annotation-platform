"""Independent package verification and bounded coordinator observations."""

import asyncio
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

import benchmark_dataset_training as bench


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-c6-helper-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "package.zip"
        self.picture = bench.payloads()[0]
        self.expected = {
            "task-a": {
                key: value for key, value in self.picture.items() if key != "data"
            }
        }
        self.manifest = {
            "samples": [
                {
                    "task_id": "task-a",
                    "sha256": self.picture["sha256"],
                    "size_bytes": self.picture["size_bytes"],
                    "width": self.picture["width"],
                    "height": self.picture["height"],
                    "split": "train",
                    "image_path": "images/train/task-a.png",
                    "label_path": "labels/train/task-a.txt",
                    "label_sha256": hashlib.sha256(bench.LABEL).hexdigest(),
                }
            ]
        }

    def write(self, *, image=None, label=None, extra=False, manifest=None):
        with zipfile.ZipFile(self.path, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest or self.manifest))
            archive.writestr("classes.json", json.dumps(["vehicle"]))
            archive.writestr("data.yaml", "train: images/train\nval: images/val\n")
            archive.writestr(
                "images/train/task-a.png",
                self.picture["data"] if image is None else image,
            )
            archive.writestr(
                "labels/train/task-a.txt", bench.LABEL if label is None else label
            )
            if extra:
                archive.writestr("unexpected.bin", b"unclaimed")

    def test_payloads_are_real_decodable_images_with_exact_dimensions_and_digests(self):
        from PIL import Image

        profiles = bench.payloads()
        self.assertEqual(len(profiles), 3)
        for profile in profiles:
            with Image.open(io.BytesIO(profile["data"])) as image:
                image.load()
                self.assertEqual(image.size, (profile["width"], profile["height"]))
            self.assertEqual(len(profile["data"]), profile["size_bytes"])
            self.assertEqual(
                hashlib.sha256(profile["data"]).hexdigest(), profile["sha256"]
            )

    def test_exact_package_and_known_bbox_labels(self):
        self.write()
        self.assertEqual(
            bench.verify_package(self.path, self.expected, self.manifest)["samples"], 1
        )

    def test_wrong_actual_image_bytes_are_rejected_even_when_manifest_is_unchanged(
        self,
    ):
        self.write(image=b"different source")
        with self.assertRaisesRegex(ValueError, "Image content mismatch"):
            bench.verify_package(self.path, self.expected, self.manifest)

    def test_wrong_labels_or_extra_members_are_rejected(self):
        for options in ({"label": b"0 0 0 1 1\n"}, {"extra": True}):
            with self.subTest(options=options):
                self.write(**options)
                with self.assertRaises(ValueError):
                    bench.verify_package(self.path, self.expected, self.manifest)

    def test_manifest_identity_substitution_is_rejected(self):
        manifest = deepcopy(self.manifest)
        manifest["samples"][0]["task_id"] = "task-b"
        self.write(manifest=manifest)
        with self.assertRaisesRegex(ValueError, "Manifest identities changed"):
            bench.verify_package(self.path, self.expected, manifest)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def test_incomplete_observation_cannot_claim_settled_resource_measurement(self):
        bench.require_complete_observation({"complete_observation": True})
        for value in ({}, {"complete_observation": False}, {"complete_observation": 1}):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "did not fully settle"),
            ):
                bench.require_complete_observation(value)

    async def test_phase_deadline_cancels_and_drains_sampler(self):
        drained = asyncio.Event()

        async def blocked(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        with tempfile.TemporaryDirectory(prefix="aap-c6-deadline-") as temporary:
            with (
                patch.object(bench, "sampled", new=blocked),
                self.assertRaises(TimeoutError),
            ):
                await bench.run_phase(
                    "build",
                    None,
                    {},
                    Path(temporary),
                    {"phase_timeout_seconds": 0.001},
                    Path(temporary) / "report.json",
                )
        self.assertTrue(drained.is_set())

    async def test_phase_report_persists_metrics_without_response_url_or_token(self):
        with tempfile.TemporaryDirectory(prefix="aap-c6-phase-") as temporary:
            path = Path(temporary) / "report.json"
            report = {}
            response = ({"url": "https://host/?signature=private"}, {"sql_count": 3})
            with patch.object(
                bench, "sampled", new=AsyncMock(return_value=(response, {"samples": 1}))
            ):
                await bench.run_phase(
                    "preflight", None, {}, Path(temporary), report, path
                )
            raw = path.read_text()
            self.assertNotIn("signature", raw)
            self.assertEqual(
                json.loads(raw)["phase_observations"][0]["metrics"]["sql_count"], 3
            )

    async def test_observer_failure_cancels_and_drains_owned_phase_coroutine(self):
        started, drained = asyncio.Event(), asyncio.Event()

        async def operation():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        class Process:
            pid = 1
            calls = 0

            def poll(self):
                self.calls += 1
                return None if self.calls == 1 else 1

        sample = {"rss_kib": 1, "cpu_seconds": 0, "temporary_bytes": 0}
        with (
            tempfile.TemporaryDirectory(prefix="aap-c6-cancel-") as temporary,
            patch.object(bench, "process_sample", return_value=sample),
        ):
            with self.assertRaisesRegex(ValueError, "measurement process stopped"):
                await bench.sampled(operation(), {"worker": Process()}, Path(temporary))
        self.assertTrue(started.is_set() and drained.is_set())


if __name__ == "__main__":
    unittest.main()
