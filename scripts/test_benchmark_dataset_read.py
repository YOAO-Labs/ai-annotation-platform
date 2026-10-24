"""Measurement primitives only; no database or object store is contacted."""

import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark_dataset_read import (
    api_dependencies_ready,
    build_fixtures,
    distribution,
    process_sample,
)
from dataset_read_benchmark_api import ObservedAPI, ObservedBody, current


class ReadBenchmarkTests(unittest.TestCase):
    def test_percentiles_are_nearest_rank_and_empty_is_not_zero(self):
        result = distribution(list(range(1, 101)))
        self.assertEqual(
            (result["count"], result["p50"], result["p95"]), (100, 50.5, 95)
        )
        with self.assertRaises(ValueError):
            distribution([])

    def test_fixture_formats_and_hashes_match_real_bytes(self):
        import hashlib
        from PIL import Image

        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "benchmark_dataset_read.PROFILES",
                [("png", (12, 8), "PNG"), ("jpeg", (24, 16), "JPEG")],
            ),
        ):
            root = Path(folder)
            profiles = build_fixtures(root)
            for profile in profiles:
                path = root / profile["filename"]
                self.assertEqual(profile["size_bytes"], path.stat().st_size)
                self.assertEqual(
                    profile["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
                )
                with Image.open(path) as image:
                    image.load()
                    self.assertEqual(image.size, profile["dimensions"])
                    self.assertEqual(image.format, profile["format"])

    def test_body_counter_delegates_without_changing_reads(self):
        metrics = {"source_read_calls": 0, "source_bytes": 0}
        wrapped = ObservedBody(io.BytesIO(b"original"), metrics)
        self.assertEqual(wrapped.read(3), b"ori")
        self.assertEqual(wrapped.read(), b"ginal")
        self.assertEqual(wrapped.read(), b"")
        self.assertEqual(wrapped.tell(), 8)
        self.assertEqual(metrics, {"source_read_calls": 3, "source_bytes": 8})

    def test_sampler_distinguishes_source_and_anonymous_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            path = source / "original"
            path.write_bytes(b"original")
            with path.open("rb"), tempfile.TemporaryFile(dir=root) as temporary:
                temporary.write(b"verified snapshot")
                temporary.flush()
                observation = process_sample(os.getpid(), source, root)
                self.assertEqual(observation["source_regular_fds"], 1)
                self.assertEqual(observation["temporary_fds"], 1)
                self.assertEqual(observation["temporary_bytes"], 17)
            observation = process_sample(os.getpid(), source, root)
            self.assertEqual(observation["source_regular_fds"], 0)
            self.assertEqual(observation["temporary_bytes"], 0)


class ObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_readiness_requires_read_dependencies_without_celery(self):
        import httpx

        for failed in (None, "db", "redis", "minio"):
            with self.subTest(failed=failed):
                paths = []

                def handler(request):
                    paths.append(request.url.path)
                    ready = request.url.path in {
                        f"/health/{name}"
                        for name in ("db", "redis", "minio")
                        if name != failed
                    }
                    return httpx.Response(200 if ready else 503)

                async with httpx.AsyncClient(
                    base_url="http://local.test", transport=httpx.MockTransport(handler)
                ) as client:
                    self.assertEqual(
                        await api_dependencies_ready(client), failed is None
                    )
                self.assertCountEqual(
                    paths, ["/health/db", "/health/redis", "/health/minio"]
                )

    async def test_request_context_reaches_threads_and_restores_after_response(self):
        with tempfile.TemporaryDirectory() as folder:

            async def app(scope, receive, send):
                await asyncio.to_thread(
                    ObservedBody(io.BytesIO(b"abc"), current.get()).read
                )
                await send({"type": "http.response.start", "status": 200})
                await send({"type": "http.response.body", "body": b"abc"})

            messages = []

            async def send(message):
                messages.append(message)

            identifier = "a" * 32
            await ObservedAPI(app, Path(folder))(
                {
                    "type": "http",
                    "path": "/private-original",
                    "headers": [
                        (b"x-read-benchmark", identifier.encode()),
                        (b"authorization", b"private-credential"),
                    ],
                },
                None,
                send,
            )
            data = json.loads((Path(folder) / f"{identifier}.json").read_text())
            self.assertEqual(data["source_bytes"], 3)
            self.assertEqual(data["status"], 200)
            self.assertNotIn("private", json.dumps(data))
            self.assertEqual(messages[-1]["body"], b"abc")
            self.assertIsNone(current.get())

    async def test_untagged_requests_are_not_recorded(self):
        with tempfile.TemporaryDirectory() as folder:

            async def app(*args):
                self.assertIsNone(current.get())

            await ObservedAPI(app, Path(folder))(
                {"type": "http", "headers": [(b"x-read-benchmark", b"../../other")]},
                None,
                None,
            )
            self.assertEqual(list(Path(folder).iterdir()), [])

    async def test_failed_request_keeps_a_settlement_record_and_resets_context(self):
        with tempfile.TemporaryDirectory() as folder:

            async def app(*args):
                raise RuntimeError("failed")

            with self.assertRaises(RuntimeError):
                await ObservedAPI(app, Path(folder))(
                    {"type": "http", "headers": [(b"x-read-benchmark", b"b" * 32)]},
                    None,
                    None,
                )
            self.assertIsNone(current.get())
            self.assertIn(
                "settled_seconds",
                json.loads(next(Path(folder).glob("*.json")).read_text()),
            )


if __name__ == "__main__":
    unittest.main()
