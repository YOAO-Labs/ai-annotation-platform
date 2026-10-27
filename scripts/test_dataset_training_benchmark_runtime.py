"""Isolated observer contracts; no API, database, broker or S3 is contacted."""

import asyncio
from contextvars import copy_context
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import dataset_training_benchmark_runtime as runtime
from worktree_env import BUCKETS


class BodyTests(unittest.TestCase):
    def test_bytes_readinto_seek_and_repeat_close_are_delegated(self):
        measurement = runtime.Measurement("a" * 32, "http")
        body = io.BytesIO(b"abcdef\n")
        wrapped = runtime.ObservedBody(body, measurement)
        self.assertEqual(wrapped.read(2), b"ab")
        target = bytearray(2)
        self.assertEqual(wrapped.readinto(target), 2)
        self.assertEqual(target, b"cd")
        self.assertEqual(wrapped.readline(), b"ef\n")
        self.assertEqual(wrapped.tell(), 7)
        wrapped.seek(0)
        self.assertEqual(wrapped.readlines(), [b"abcdef\n"])
        wrapped.close()
        wrapped.close()
        values = measurement.finish()
        self.assertEqual(values["body_read_bytes"], 14)
        self.assertEqual(values["body_close_calls"], 2)
        self.assertEqual(values["body_close_successes"], 1)
        self.assertEqual(values["body_unclosed_count"], 0)
        self.assertTrue(body.closed)
        self.assertTrue(values["complete_observation"])

    def test_line_iterator_counts_newlines_that_are_absent_from_yielded_lines(self):
        measurement = runtime.Measurement("a" * 32, "http")
        wrapped = runtime.ObservedBody(
            io.BytesIO(b"first\r\nsecond\nlast"), measurement
        )
        self.assertEqual(
            list(wrapped.iter_lines(chunk_size=3)), [b"first", b"second", b"last"]
        )
        wrapped.close()
        self.assertEqual(measurement.finish()["body_read_bytes"], 18)

    def test_native_iteration_preserves_native_line_semantics(self):
        measurement = runtime.Measurement("a" * 32, "http")
        wrapped = runtime.ObservedBody(io.BytesIO(b"one\ntwo\n"), measurement)
        self.assertEqual(list(wrapped), [b"one\n", b"two\n"])
        wrapped.close()
        self.assertEqual(measurement.finish()["body_read_bytes"], 8)

    def test_context_manager_wraps_returned_raw_stream_and_preserves_exit_result(self):
        raw = io.BytesIO(b"raw stream")

        class Native:
            def __enter__(self):
                return raw

            def __exit__(self, *arguments):
                raw.close()
                return True

        measurement = runtime.Measurement("a" * 32, "http")
        wrapped = runtime.ObservedBody(Native(), measurement)
        with wrapped as entered:
            self.assertEqual(entered.read(), b"raw stream")
            raise LookupError("suppressed by native context")
        values = measurement.finish()
        self.assertEqual(values["body_read_bytes"], 10)
        self.assertEqual(values["body_open_count"], 1)
        self.assertEqual(values["body_close_successes"], 1)
        self.assertTrue(raw.closed)

    def test_read_and_close_failures_preserve_exact_exceptions_and_unknown_close(self):
        read_error, close_error = OSError("private read"), RuntimeError("private close")
        native = SimpleNamespace(
            read=Mock(side_effect=read_error), close=Mock(side_effect=close_error)
        )
        measurement = runtime.Measurement("a" * 32, "http")
        wrapped = runtime.ObservedBody(native, measurement)
        with self.assertRaises(OSError) as reading:
            wrapped.read(1)
        self.assertIs(reading.exception, read_error)
        with self.assertRaises(RuntimeError) as closing:
            wrapped.close()
        self.assertIs(closing.exception, close_error)
        values = measurement.finish()
        self.assertEqual(values["body_read_failed_calls"], 1)
        self.assertEqual(values["body_close_failed_calls"], 1)
        self.assertEqual(values["body_unclosed_count"], 1)
        self.assertFalse(values["complete_observation"])
        self.assertNotIn("private", json.dumps(values))

    def test_unfinished_body_and_sql_make_observation_incomplete(self):
        measurement = runtime.Measurement("a" * 32, "http")
        native = io.BytesIO(b"pending")
        runtime.ObservedBody(native, measurement)
        measurement.add(sql_inflight=1)
        self.assertFalse(measurement.finish()["complete_observation"])
        native.close()

    def test_unknown_read_keyword_still_reaches_native_and_raises(self):
        measurement = runtime.Measurement("a" * 32, "http")
        wrapped = runtime.ObservedBody(io.BytesIO(b"value"), measurement)
        with self.assertRaises(TypeError):
            wrapped.read(readinto=True)
        wrapped.close()
        self.assertEqual(measurement.finish()["body_read_failed_calls"], 1)

    def test_originating_measurement_keeps_body_when_context_changes(self):
        first, second = (
            runtime.Measurement("a" * 32, "http"),
            runtime.Measurement("b" * 32, "http"),
        )
        body = runtime.ObservedBody(io.BytesIO(b"first"), first)
        token = runtime.current.set(second)
        try:
            self.assertEqual(body.read(), b"first")
            body.close()
        finally:
            runtime.current.reset(token)
        self.assertEqual(first.finish()["body_read_bytes"], 5)
        self.assertEqual(second.finish()["body_read_bytes"], 0)


class CallbackTests(unittest.TestCase):
    def test_success_and_failed_sql_are_counted_once_without_retaining_arguments(self):
        measurement = runtime.Measurement("a" * 32, "http")
        token = runtime.current.set(measurement)
        context = SimpleNamespace()
        try:
            runtime.sql_before(
                None, None, "private query", {"secret": "value"}, context, False
            )
            runtime.sql_after(None, None, None, None, context, False)
            runtime.sql_before(None, None, "second private query", None, context, False)
            runtime.sql_error(SimpleNamespace(execution_context=context))
            runtime.sql_error(SimpleNamespace(execution_context=context))
        finally:
            runtime.current.reset(token)
        values = measurement.finish()
        self.assertEqual(values["sql_count"], 2)
        self.assertEqual(values["sql_failed_count"], 1)
        self.assertEqual(values["sql_inflight"], 0)
        self.assertNotIn("private", json.dumps(values))
        self.assertNotIn("secret", json.dumps(values))

    def test_no_context_and_non_s3_sdk_calls_are_untouched(self):
        response = {"Body": io.BytesIO(b"not observed")}
        original = Mock(return_value=response)
        client = SimpleNamespace(
            meta=SimpleNamespace(service_model=SimpleNamespace(service_name="s3"))
        )
        self.assertIs(
            runtime.observe_s3_call(original, client, "GetObject", {}), response
        )
        measurement = runtime.Measurement("a" * 32, "http")
        token = runtime.current.set(measurement)
        try:
            client.meta.service_model.service_name = "sts"
            self.assertIs(
                runtime.observe_s3_call(original, client, "GetCallerIdentity", {}),
                response,
            )
        finally:
            runtime.current.reset(token)
        response["Body"].close()
        self.assertEqual(measurement.finish()["s3_calls"], 0)

    def test_s3_success_failure_and_body_counts_do_not_retain_keys_or_lengths(self):
        measurement = runtime.Measurement("a" * 32, "http")
        client = SimpleNamespace(
            meta=SimpleNamespace(service_model=SimpleNamespace(service_name="s3"))
        )
        original = Mock(
            return_value={"ContentLength": 999, "Body": io.BytesIO(b"actual")}
        )
        token = runtime.current.set(measurement)
        try:
            result = runtime.observe_s3_call(
                original, client, "GetObject", {"Key": "private-key"}
            )
            self.assertEqual(result["Body"].read(), b"actual")
            result["Body"].close()
            error = OSError("private sdk cause")
            original.side_effect = error
            with self.assertRaises(OSError) as caught:
                runtime.observe_s3_call(
                    original, client, "HeadObject", {"Key": "private-key"}
                )
            self.assertIs(caught.exception, error)
        finally:
            runtime.current.reset(token)
        values = measurement.finish()
        self.assertEqual(values["s3_calls"], 2)
        self.assertEqual(values["s3_failed_calls"], 1)
        self.assertEqual(values["body_read_bytes"], 6)
        self.assertEqual(values["s3_inflight"], 0)
        self.assertNotIn("private", json.dumps(values))

    def test_inspection_propagates_value_and_error_and_restores_no_context_behavior(
        self,
    ):
        sentinel = object()
        original = Mock(return_value=sentinel)
        self.assertIs(runtime.observe_inspection(original, 1, mode="x"), sentinel)
        measurement = runtime.Measurement("a" * 32, "http")
        token = runtime.current.set(measurement)
        try:
            self.assertIs(runtime.observe_inspection(original, 1), sentinel)
            original.side_effect = ValueError("private")
            with self.assertRaises(ValueError):
                runtime.observe_inspection(original, 2)
        finally:
            runtime.current.reset(token)
        values = measurement.finish()
        self.assertEqual(values["image_inspection_count"], 2)
        self.assertEqual(values["image_inspection_failed_count"], 1)


class ScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-training-observer-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    async def test_http_context_reaches_threads_records_actual_status_and_restores_outer(
        self,
    ):
        outer = runtime.Measurement("b" * 32, "http")
        outer_token = runtime.current.set(outer)

        async def app(scope, receive, send):
            def reading():
                body = runtime.ObservedBody(
                    io.BytesIO(b"thread"), runtime.current.get()
                )
                body.read()
                body.close()

            await asyncio.to_thread(reading)
            await send({"type": "http.response.start", "status": 202})
            await send({"type": "http.response.body", "body": b"response"})
            return "same app result"

        messages = []

        async def send(message):
            messages.append(message)

        try:
            result = await runtime.ObservedAPI(app, self.directory)(
                {"type": "http", "headers": [(b"x-training-benchmark", b"a" * 32)]},
                None,
                send,
            )
            self.assertIs(runtime.current.get(), outer)
        finally:
            runtime.current.reset(outer_token)
        self.assertEqual(result, "same app result")
        record = json.loads((self.directory / ("a" * 32 + ".json")).read_text())
        self.assertEqual(record["body_read_bytes"], 6)
        self.assertEqual(record["status"], 202)
        self.assertEqual(len(messages), 2)
        self.assertTrue(record["complete_observation"])
        self.assertEqual(
            (self.directory / ("a" * 32 + ".json")).stat().st_mode & 0o777, 0o600
        )

    async def test_untagged_invalid_or_duplicate_header_does_not_inherit_outer(self):
        observed = []

        async def app(scope, receive, send):
            observed.append(runtime.current.get())

        outer_token = runtime.current.set(runtime.Measurement("b" * 32, "http"))
        try:
            for headers in (
                [],
                [(b"x-training-benchmark", b"a" * 32 + b"\xff")],
                [(b"x-training-benchmark", b"a" * 32)] * 2,
            ):
                await runtime.ObservedAPI(app, self.directory)(
                    {"type": "http", "headers": headers}, None, None
                )
        finally:
            runtime.current.reset(outer_token)
        self.assertEqual(observed, [None, None, None])
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_app_exception_is_identical_and_safe_record_is_written(self):
        error = RuntimeError("private cause")

        async def app(scope, receive, send):
            raise error

        with self.assertRaises(RuntimeError) as caught:
            await runtime.ObservedAPI(app, self.directory)(
                {"type": "http", "headers": [(b"x-training-benchmark", b"a" * 32)]},
                None,
                None,
            )
        self.assertIs(caught.exception, error)
        self.assertIsNone(runtime.current.get())
        record = json.loads((self.directory / ("a" * 32 + ".json")).read_text())
        self.assertTrue(record["failed"])
        self.assertEqual(record["error_type"], "RuntimeError")
        self.assertNotIn("private", json.dumps(record))

    async def test_persistence_failure_does_not_replace_original_app_error(self):
        error = LookupError("application")

        async def app(scope, receive, send):
            raise error

        with patch.object(runtime, "persist", side_effect=OSError("disk")):
            with self.assertRaises(LookupError) as caught:
                await runtime.ObservedAPI(app, self.directory)(
                    {"type": "http", "headers": [(b"x-training-benchmark", b"a" * 32)]},
                    None,
                    None,
                )
        self.assertIs(caught.exception, error)

    async def test_task_measurement_suppresses_nested_other_tasks_and_restores_context(
        self,
    ):
        observer = runtime.TrainingTaskObserver(self.directory)
        target = SimpleNamespace(name=runtime.TRAINING_TASK)
        other = SimpleNamespace(name="app.workers.other")
        task_id = "01234567-89ab-cdef-0123-456789abcdef"
        observer.begin(sender=target, task_id=task_id)
        measured = runtime.current.get()
        measured.add(sql_count=2)
        observer.begin(sender=other, task_id="ignored")
        self.assertIsNone(runtime.current.get())
        observer.finish(sender=other, task_id="ignored", state="SUCCESS")
        self.assertIs(runtime.current.get(), measured)
        observer.finish(sender=other, task_id="unmatched", state="SUCCESS")
        self.assertIs(runtime.current.get(), measured)
        observer.finish(sender=target, task_id=task_id, state="SUCCESS")
        self.assertIsNone(runtime.current.get())
        record = json.loads((self.directory / (task_id + ".json")).read_text())
        self.assertEqual(record["sql_count"], 2)
        self.assertEqual(record["task_name"], runtime.TRAINING_TASK)
        self.assertFalse(record["failed"])
        self.assertEqual(len(list(self.directory.iterdir())), 1)

    async def test_task_failure_and_invalid_id_cannot_escape_report_directory(self):
        observer = runtime.TrainingTaskObserver(self.directory)
        target = SimpleNamespace(name=runtime.TRAINING_TASK)
        with self.assertRaises(ValueError):
            observer.begin(sender=target, task_id="../escape")
        observer.begin(sender=target, task_id="a" * 32)
        observer.finish(sender=target, task_id="a" * 32, state="FAILURE")
        self.assertTrue(
            json.loads((self.directory / ("a" * 32 + ".json")).read_text())["failed"]
        )


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.resources = {
            "database": "owned_test",
            "owner": "owned-marker",
            "redis_name": "owned-redis",
            "buckets": {
                key: "owned-" + str(index) for index, key in enumerate(BUCKETS)
            },
        }
        self.manifest = {
            "resources": self.resources,
            "postgres": {"host": "127.0.0.1", "port": 5432},
        }
        self.environment = {
            "AAP_WORKTREE_MODE": "test",
            "DATABASE_URL": "postgresql+asyncpg://runtime:private@127.0.0.1:5432/owned_test",
            **self.resources["buckets"],
        }

    def contract(self, environ=None):
        with (
            patch("worktree_env.load_identity", return_value={}),
            patch("worktree_env.topology", return_value=self.resources),
            patch("worktree_env.read_state", return_value=self.manifest),
        ):
            return runtime.environment_contract(
                Path("/unused"), self.environment if environ is None else environ
            )

    def test_exact_test_mode_manifest_database_and_nine_buckets_are_required(self):
        self.assertIs(self.contract(), self.resources)
        changes = [
            {"AAP_WORKTREE_MODE": "e2e"},
            {
                "DATABASE_URL": "postgresql+asyncpg://runtime:private@remote:5432/owned_test"
            },
            {
                "DATABASE_URL": "postgresql+asyncpg://runtime:private@127.0.0.1:5432/other"
            },
            {"MINIO_BUCKET": "other-bucket"},
            {"MIGRATION_DATABASE_URL": "private-owner"},
            {"test_database_url": "private-owner"},
        ]
        for changed in changes:
            with self.subTest(changed=list(changed)):
                with self.assertRaises(ValueError) as caught:
                    self.contract({**self.environment, **changed})
                self.assertNotIn("private", str(caught.exception))
        with patch.dict(self.manifest, {"resources": {}}):
            with self.assertRaisesRegex(ValueError, "manifest"):
                self.contract()

    def test_db_owner_role_or_wrong_database_cannot_claim_ordinary_runtime(self):
        valid = {"database": "owned_test", "owner": "owned-marker", "privileged": False}
        runtime.check_runtime_role(valid, self.resources)
        for changes in (
            {"privileged": True},
            {"owner": "foreign"},
            {"database": "wrong"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "ordinary_owned"):
                    runtime.check_runtime_role({**valid, **changes}, self.resources)

    def test_owned_inventory_is_inspected_and_closed_without_provisioning(self):
        owned = {"owner": "owned-marker"}
        backend = SimpleNamespace(
            inspect=Mock(
                return_value={
                    "database": owned,
                    "redis": owned,
                    "buckets": dict.fromkeys(self.resources["buckets"].values(), owned),
                }
            ),
            environment=Mock(return_value={}),
            s3=SimpleNamespace(close=Mock()),
        )
        settings = SimpleNamespace(database_url=self.environment["DATABASE_URL"])
        role = AsyncMock()
        with (
            patch.object(runtime, "environment_contract", return_value=self.resources),
            patch.dict(
                "sys.modules",
                {
                    "app.config": SimpleNamespace(settings=settings),
                    "worktree_resources": SimpleNamespace(
                        LocalResources=Mock(return_value=backend)
                    ),
                },
            ),
            patch("benchmark_dataset_scan.check_worker_environment") as check,
            patch.object(runtime, "verify_runtime_role", role),
        ):
            self.assertIs(runtime.verify_runtime_environment(), self.resources)
        backend.inspect.assert_called_once_with()
        check.assert_called_once()
        role.assert_awaited_once_with(settings.database_url, self.resources)
        backend.s3.close.assert_called_once_with()

    def test_mismatched_inventory_still_closes_observer_client(self):
        backend = SimpleNamespace(
            inspect=Mock(
                return_value={
                    "database": {"owner": "foreign"},
                    "redis": None,
                    "buckets": {},
                }
            ),
            s3=SimpleNamespace(close=Mock()),
        )
        with (
            patch.object(runtime, "environment_contract", return_value=self.resources),
            patch.dict(
                "sys.modules",
                {
                    "app.config": SimpleNamespace(settings=SimpleNamespace()),
                    "worktree_resources": SimpleNamespace(
                        LocalResources=Mock(return_value=backend)
                    ),
                },
            ),
        ):
            with self.assertRaises(ValueError):
                runtime.verify_runtime_environment()
        backend.s3.close.assert_called_once_with()


class PersistenceTests(unittest.TestCase):
    def test_existing_measurement_is_not_replaced_and_pending_file_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="aap-training-persist-") as temporary:
            directory = Path(temporary)
            record = runtime.Measurement("a" * 32, "task").finish()
            runtime.persist(directory, record)
            original = (directory / ("a" * 32 + ".json")).read_bytes()
            with self.assertRaises(FileExistsError):
                runtime.persist(directory, {**record, "failed": True})
            self.assertEqual((directory / ("a" * 32 + ".json")).read_bytes(), original)
            self.assertEqual(len(list(directory.iterdir())), 1)

    def test_context_copy_shares_counter_across_threads_without_exposing_other_context(
        self,
    ):
        measurement = runtime.Measurement("a" * 32, "http")
        token = runtime.current.set(measurement)
        copied = copy_context()
        runtime.current.reset(token)
        copied.run(lambda: runtime.current.get().add(sql_count=1))
        self.assertIsNone(runtime.current.get())
        self.assertEqual(measurement.finish()["sql_count"], 1)


if __name__ == "__main__":
    unittest.main()
