"""Operator bridge ordering, durable ownership and safe terminal boundaries."""

import argparse
from contextlib import asynccontextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

import finalize_storage_migration as cli
from storage_source_fence_state import FenceError, Journal

DIGEST = "a" * 64
ACTOR, WINDOW, FINAL, NATIVE, COPY = (str(uuid.uuid4()) for _ in range(5))
SOURCE = {
    "endpoint": "http://127.0.0.1:19000",
    "purpose_buckets": {"training": "owned"},
}


class ArgumentsTests(unittest.TestCase):
    def test_argument_error_does_not_expose_supplied_values(self):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            result = cli.main(["sensitive-unsupported-argument"])
        self.assertEqual(result, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertNotIn("sensitive", error.getvalue())
        self.assertEqual(json.loads(error.getvalue())["code"], "invalid_arguments")

    def test_zero_revision_is_only_accepted_for_explicit_abandon(self):
        common = [
            "--config",
            "private",
            "--state",
            "state",
            "--actor-id",
            ACTOR,
            "--finalization-id",
            FINAL,
        ]
        args = cli.parser().parse_args(
            ["invalidate", *common, "--expected-revision", "0"]
        )
        self.assertEqual(args.expected_revision, 0)
        with self.assertRaises(FenceError):
            cli.parser().parse_args(
                ["seal", *common, "--copy-id", COPY, "--expected-revision", "0"]
            )

    def test_safe_error_only_accepts_closed_codes(self):
        for failure in (
            ValueError("secret-url"),
            SimpleNamespace(code="secret:token"),
            SimpleNamespace(detail="private"),
        ):
            self.assertEqual(cli.safe_error(failure), "finalization_failed")
        self.assertEqual(
            cli.safe_error(SimpleNamespace(detail={"code": "finalization_changed"})),
            "finalization_changed",
        )

    def test_stable_native_identity_ignores_observation_clock_and_revision(self):
        before = dict(
            config_digest=DIGEST,
            operation_id=NATIVE,
            barriers_sha256=DIGEST,
            source=SOURCE,
            counts={"versions": 1},
            journal_revision=2,
            observed_at="before",
        )
        after = {**before, "journal_revision": 4, "observed_at": "after"}
        cli.stable_native(before, after)
        for key in (
            "config_digest",
            "operation_id",
            "barriers_sha256",
            "source",
            "counts",
        ):
            with self.subTest(key=key), self.assertRaises(FenceError):
                cli.stable_native(before, {**after, key: None})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            prefix="aap-finalization-cli-test-"
        )
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "fence-state.json"
        self.journal = Journal(self.path, DIGEST, create=True).__enter__()
        self.addCleanup(self.journal.__exit__)
        self.journal.data.update(
            phase="held",
            operation_id=NATIVE,
            snapshot={},
            barriers=[{}, {}],
            observation={
                "observed_at": "2026-10-26T00:00:00+00:00",
                "counts": {"versions": 1},
            },
        )
        self.journal.save()
        self.events = []
        self.commit_number = 0
        self.fail_commit = None
        self.result = SimpleNamespace(state="bound", revision=1, window_revision=3)
        self.config = {
            "public_endpoint": SOURCE["endpoint"],
            "purpose_buckets": SOURCE["purpose_buckets"],
        }
        self.native = {
            "config_digest": DIGEST,
            "operation_id": NATIVE,
            "journal_revision": self.journal.data["revision"],
            "barriers_sha256": DIGEST,
            "observed_at": "2026-10-26T00:00:00+00:00",
            "counts": {"versions": 1},
            "source": deepcopy(SOURCE),
        }
        self.service = SimpleNamespace(
            **{
                name: AsyncMock(return_value=self.result)
                for name in (
                    "bind",
                    "begin_seal",
                    "finish_seal",
                    "begin_invalidation",
                    "finish_invalidation",
                    "status",
                )
            }
        )
        self.service.preflight = AsyncMock(
            return_value={"database_identity": DIGEST, "source": deepcopy(SOURCE)}
        )
        self.adapter = Mock()

    @asynccontextmanager
    async def session(self):
        self.events.append("transaction_open")

        async def commit():
            self.commit_number += 1
            self.events.append("commit")
            if self.commit_number == self.fail_commit:
                raise OSError("private-commit-connection")

        try:
            yield SimpleNamespace(commit=commit)
        finally:
            self.events.append("transaction_closed")

    async def settled(self, function, *args, **kwargs):
        self.assertEqual(self.events[-1], "transaction_closed")
        self.events.append("evidence")
        return function(*args, **kwargs)

    def args(self, command):
        return argparse.Namespace(
            command=command,
            actor_id=ACTOR,
            finalization_id=FINAL,
            window_id=WINDOW,
            expected_window_revision=2,
            expected_native_revision=self.journal.data["revision"],
            copy_id=COPY,
            expected_revision=1,
        )

    async def run_bridge(self, args, *, observations=None):
        evidence = Mock(return_value={"evidence_sha256": DIGEST})
        with (
            patch.object(
                cli,
                "application",
                return_value=(self.service, self.session, self.settled, evidence),
            ),
            patch.object(
                cli,
                "native_proof",
                side_effect=observations or (lambda *_: deepcopy(self.native)),
            ),
        ):
            return await cli.execute(args, self.journal, self.adapter, self.config)

    def attach(self):
        claim = {
            "schema": cli.CLAIM_SCHEMA,
            "finalization_id": FINAL,
            "window_id": WINDOW,
            "expected_window_revision": 2,
            "database_identity": DIGEST,
            "reservation_nonce": str(uuid.uuid4()),
            "native_operation_id": NATIVE,
            "config_digest": DIGEST,
            "reserved": False,
        }
        self.journal.bind_application(claim)
        return claim

    async def test_bind_persists_claim_before_database_registration(self):
        async def register(db, actor, claim, native):
            saved = json.loads(self.path.read_bytes())["application_claim"]
            self.assertEqual(saved, claim)
            self.assertFalse(saved["reserved"])
            self.assertEqual(actor, uuid.UUID(ACTOR))
            return self.result

        self.service.bind.side_effect = register
        await self.run_bridge(self.args("bind"))
        self.assertTrue(
            json.loads(self.path.read_bytes())["application_claim"]["reserved"]
        )
        self.adapter.assert_not_called()

    async def test_unknown_bind_commit_keeps_unacknowledged_claim(self):
        self.fail_commit = 1
        with self.assertRaises(OSError):
            await self.run_bridge(self.args("bind"))
        claim = json.loads(self.path.read_bytes())["application_claim"]
        self.assertFalse(claim["reserved"])
        self.assertEqual(claim["finalization_id"], FINAL)

    async def test_bind_resumes_same_intent_without_regenerating_nonce(self):
        claim = self.attach()
        await self.run_bridge(self.args("bind"))
        self.service.preflight.assert_not_called()
        self.assertEqual(
            self.journal.data["application_claim"]["reservation_nonce"],
            claim["reservation_nonce"],
        )
        self.assertTrue(self.journal.data["application_claim"]["reserved"])

    async def test_wrong_physical_source_does_not_create_application_claim(self):
        self.service.preflight.return_value["source"]["endpoint"] = (
            "http://127.0.0.1:19001"
        )
        with self.assertRaises(FenceError):
            await self.run_bridge(self.args("bind"))
        self.assertIsNone(self.journal.data.get("application_claim"))
        self.service.bind.assert_not_called()

    async def test_stale_native_revision_does_not_touch_database(self):
        args = self.args("bind")
        args.expected_native_revision -= 1
        with self.assertRaises(FenceError):
            await self.run_bridge(args)
        self.service.preflight.assert_not_called()

    async def test_seal_closes_freeze_transaction_before_file_and_native_checks(self):
        self.attach()
        self.service.begin_seal.return_value = {
            "item": self.result,
            "values": {"bounded": True},
            "token": {"sequence": 2},
        }
        await self.run_bridge(self.args("seal"))
        self.assertEqual(
            self.events,
            [
                "transaction_open",
                "commit",
                "transaction_closed",
                "evidence",
                "transaction_open",
                "commit",
                "transaction_closed",
            ],
        )
        self.assertEqual(
            self.service.finish_seal.await_args.args[-2], {"evidence_sha256": DIGEST}
        )
        self.assertIsNotNone(self.journal.data.get("application_claim"))

    async def test_changed_native_observation_never_commits_seal(self):
        self.attach()
        self.service.begin_seal.return_value = {
            "item": self.result,
            "values": {},
            "token": {},
        }
        with self.assertRaises(FenceError):
            await self.run_bridge(
                self.args("seal"),
                observations=[self.native, {**self.native, "counts": {"versions": 2}}],
            )
        self.service.finish_seal.assert_not_called()
        self.assertIsNotNone(self.journal.data.get("application_claim"))

    async def test_unsettled_invalidation_keeps_release_interlock(self):
        self.attach()
        self.service.begin_invalidation.return_value = SimpleNamespace(
            state="invalidating", revision=2
        )
        self.service.finish_invalidation.side_effect = FenceError(
            "finalization_unsettled"
        )
        with self.assertRaises(FenceError):
            await self.run_bridge(self.args("invalidate"))
        self.assertIsNotNone(self.journal.data.get("application_claim"))

    async def test_unknown_invalidation_commit_never_removes_claim(self):
        self.attach()
        self.service.begin_invalidation.return_value = SimpleNamespace(
            state="invalidating", revision=2
        )
        self.service.finish_invalidation.return_value = SimpleNamespace(
            state="invalidated", revision=3
        )
        self.fail_commit = 2
        with self.assertRaises(OSError):
            await self.run_bridge(self.args("invalidate"))
        self.assertIsNotNone(
            json.loads(self.path.read_bytes()).get("application_claim")
        )

    async def test_committed_invalidation_unbinds_without_opening_native_source(self):
        self.attach()
        self.service.begin_invalidation.return_value = SimpleNamespace(
            state="invalidated", revision=3
        )
        await self.run_bridge(self.args("invalidate"))
        self.service.finish_invalidation.assert_not_called()
        self.assertIsNone(self.journal.data.get("application_claim"))
        self.adapter.assert_not_called()

    async def test_wrong_finalization_does_not_query_database(self):
        self.attach()
        args = self.args("status")
        args.finalization_id = str(uuid.uuid4())
        with self.assertRaises(FenceError):
            await self.run_bridge(args)
        self.service.status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
