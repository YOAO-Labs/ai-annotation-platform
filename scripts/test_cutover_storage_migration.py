"""Exercise trusted cutover I/O ordering and resumable release boundaries."""

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

import cutover_storage_migration as cli
from storage_source_fence_state import FenceError

ACTOR, CUTOVER, WINDOW, NATIVE_A, NATIVE_B, NONCE = (
    str(uuid.uuid4()) for _ in range(6)
)
DIGEST = "a" * 64
SOURCE_CLAIM = {
    "schema": "aap.storage-migration-fence-claim/v1",
    "finalization_id": CUTOVER,
    "window_id": WINDOW,
    "expected_window_revision": 2,
    "database_identity": DIGEST,
    "reservation_nonce": NONCE,
    "native_operation_id": NATIVE_A,
    "config_digest": DIGEST,
    "reserved": False,
}
B_CLAIM = {
    "schema": cli.ROLLBACK_CLAIM_SCHEMA,
    "cutover_id": CUTOVER,
    "expected_cutover_revision": 2,
    "database_identity": DIGEST,
    "reservation_nonce": str(uuid.uuid4()),
    "native_operation_id": NATIVE_B,
    "config_digest": DIGEST,
    "reserved": False,
}
B_SOURCE = {"endpoint": "http://172.25.1.2:80", "purpose_buckets": {"training": "b"}}
NATIVE = {
    "config_digest": DIGEST,
    "operation_id": NATIVE_B,
    "journal_revision": 4,
    "barriers_sha256": DIGEST,
    "observed_at": "2026-10-26T00:00:00+00:00",
    "counts": {"versions": 1},
    "source": B_SOURCE,
}


class InputTests(unittest.TestCase):
    def test_parser_and_error_output_do_not_echo_untrusted_values(self):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(cli.main(["secret-url-with-token"]), 2)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(json.loads(error.getvalue())["code"], "invalid_arguments")
        self.assertNotIn("secret", error.getvalue())

    def test_status_is_independent_of_native_claim_or_config(self):
        args = cli.parser().parse_args(
            ["status", "--actor-id", ACTOR, "--cutover-id", CUTOVER]
        )
        self.assertFalse(hasattr(args, "config"))
        self.assertFalse(hasattr(args, "target_config"))

    def test_rollback_requires_positive_exact_revision(self):
        for revision in ("0", "-1", "nan"):
            with self.subTest(revision=revision), self.assertRaises(FenceError):
                cli.parser().parse_args(
                    [
                        "begin-rollback",
                        "--actor-id",
                        ACTOR,
                        "--cutover-id",
                        CUTOVER,
                        "--expected-revision",
                        revision,
                    ]
                )

    def test_review_is_private_exclusive_and_token_is_not_displayed(self):
        with tempfile.TemporaryDirectory(
            prefix="aap-cutover-review-test-"
        ) as directory:
            path = Path(directory) / "review.json"
            review = {"context": {"id": CUTOVER}, "impact_token": "private-token"}
            cli.write_review(path, review)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cli.descriptor(path)[0], review)
            with self.assertRaises(FileExistsError):
                cli.write_review(path, {"replace": True})
            self.assertEqual(json.loads(path.read_text()), review)
            self.assertNotIn(
                "impact_token", cli.public_item({"review": review})["review"]
            )

    def test_review_rejects_shared_parent_or_symlink(self):
        with tempfile.TemporaryDirectory(
            prefix="aap-cutover-review-test-"
        ) as directory:
            parent = Path(directory)
            parent.chmod(0o755)
            with self.assertRaises(FenceError):
                cli.write_review(parent / "review.json", {})
            parent.chmod(0o700)
            (parent / "link.json").symlink_to(parent / "unowned")
            with self.assertRaises(FileExistsError):
                cli.write_review(parent / "link.json", {})
            self.assertFalse((parent / "unowned").exists())

    def test_target_proof_uses_fresh_validated_application_alias(self):
        adapter, journal = Mock(), Mock()
        adapter.application_endpoint.side_effect = [B_SOURCE["endpoint"]] * 2
        proof = deepcopy(NATIVE)
        proof["source"]["endpoint"] = "http://127.0.0.1:12345"
        with patch.object(cli, "native_proof", return_value=proof):
            self.assertEqual(
                cli.target_native_proof(journal, adapter, {})["source"], B_SOURCE
            )
        adapter.application_endpoint.side_effect = [
            B_SOURCE["endpoint"],
            "http://172.25.1.3:80",
        ]
        with (
            patch.object(cli, "native_proof", return_value=deepcopy(proof)),
            self.assertRaises(FenceError),
        ):
            cli.target_native_proof(journal, adapter, {})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-cutover-cli-test-")
        self.addCleanup(self.temporary.cleanup)
        self.events, self.commits, self.fail_commit = [], 0, None
        self.a = SimpleNamespace(
            data={
                "application_claim": deepcopy(SOURCE_CLAIM),
                "revision": 7,
                "operation_id": NATIVE_A,
            },
            config_digest=DIGEST,
        )
        self.b = SimpleNamespace(data={"revision": 7}, config_digest=DIGEST)

        def bind(claim):
            self.events.append("fsync_b_claim")
            self.b.data["rollback_claim"] = deepcopy(claim)

        def mark(*_):
            self.events.append("fsync_b_reserved")
            self.b.data["rollback_claim"]["reserved"] = True

        def unbind(*_):
            self.events.append("unbind_a")
            self.a.data.pop("application_claim", None)

        self.b.bind_rollback, self.b.mark_rollback_reserved = bind, mark
        self.a.unbind_application = unbind
        self.group = {
            "id": CUTOVER,
            "state": "rollback_bound",
            "revision": 3,
            "window_id": WINDOW,
            "source_claim": deepcopy(SOURCE_CLAIM),
            "rollback_claim": deepcopy(B_CLAIM),
            "source_release_operation_id": None,
        }
        self.service = SimpleNamespace(
            **{
                name: AsyncMock(return_value=deepcopy(self.group))
                for name in (
                    "status",
                    "activate",
                    "begin_rollback",
                    "bind_rollback",
                    "finish_rollback",
                )
            }
        )
        self.service.rollback_preflight = AsyncMock(
            return_value={
                "database_identity": DIGEST,
                "target_source": deepcopy(B_SOURCE),
                "revision": 2,
            }
        )

        async def begin(db, actor, cutover_id, revision, release_id):
            self.events.append("save_release_id")
            if self.group["state"] == "rollback_bound":
                self.group.update(
                    state="rollback_restoring",
                    revision=self.group["revision"] + 1,
                    source_release_operation_id=release_id,
                )
            else:
                self.assertEqual(release_id, self.group["source_release_operation_id"])
            return deepcopy(self.group)

        self.service.begin_restoration = AsyncMock(side_effect=begin)
        self.reads = SimpleNamespace(
            status=AsyncMock(return_value=SimpleNamespace(state="active", revision=1)),
            begin_withdrawal=AsyncMock(
                return_value=SimpleNamespace(state="withdrawing", revision=2)
            ),
            finish_withdrawal=AsyncMock(
                return_value=SimpleNamespace(state="withdrawn", revision=3)
            ),
        )
        self.finals = SimpleNamespace(
            status=AsyncMock(return_value=SimpleNamespace(state="sealed", revision=3)),
            begin_invalidation=AsyncMock(
                return_value=SimpleNamespace(state="invalidating", revision=4)
            ),
            finish_invalidation=AsyncMock(
                return_value=SimpleNamespace(state="invalidated", revision=5)
            ),
        )

        def release(revision, operation_id):
            self.events.append("release_a")
            self.assertEqual(operation_id, self.group["source_release_operation_id"])
            self.assertNotIn("application_claim", self.a.data)

        self.controller = Mock(return_value=SimpleNamespace(release=release))
        self.close = AsyncMock()

    @asynccontextmanager
    async def session(self):
        self.events.append("open")

        async def commit():
            self.commits += 1
            self.events.append("commit")
            if self.fail_commit == self.commits:
                raise OSError("private-unknown-commit")

        try:
            yield SimpleNamespace(commit=commit)
        finally:
            self.events.append("closed")

    async def settled(self, function, *args, **kwargs):
        self.assertNotEqual(self.events[-1:] or [None], ["open"])
        return function(*args, **kwargs)

    def args(self, command):
        return argparse.Namespace(
            command=command,
            actor_id=ACTOR,
            cutover_id=CUTOVER,
            expected_revision=3,
            expected_finalization_revision=3,
            expected_publication_revision=1,
            review_file=Path(self.temporary.name) / "review.json",
        )

    async def run_bridge(self, args):
        with (
            patch.object(
                cli,
                "application",
                return_value=(
                    self.service,
                    self.finals,
                    self.reads,
                    Mock(),
                    self.session,
                    self.settled,
                ),
            ),
            patch.object(cli, "native_proof", return_value=deepcopy(NATIVE)),
            patch.object(cli, "target_native_proof", return_value=deepcopy(NATIVE)),
            patch.object(cli, "Controller", self.controller),
            patch.object(cli, "close_source_window", self.close),
            patch.object(cli, "same_source"),
        ):
            return await cli.execute(args, (self.a, Mock(), {}), (self.b, Mock(), {}))

    async def test_binding_fsync_precedes_registration_and_reserved_follows_commit(
        self,
    ):
        async def register(db, *_):
            self.assertIn("rollback_claim", self.b.data)
            self.assertFalse(self.b.data["rollback_claim"]["reserved"])
            self.events.append("bind_b_database")
            return deepcopy(self.group)

        self.service.bind_rollback.side_effect = register
        await self.run_bridge(self.args("bind-rollback"))
        self.assertLess(
            self.events.index("fsync_b_claim"), self.events.index("bind_b_database")
        )
        self.assertEqual(self.events[-1], "fsync_b_reserved")

    async def test_unknown_binding_commit_keeps_unconfirmed_intent(self):
        self.fail_commit = 1
        with self.assertRaises(OSError):
            await self.run_bridge(self.args("bind-rollback"))
        self.assertFalse(self.b.data["rollback_claim"]["reserved"])
        self.assertNotIn("fsync_b_reserved", self.events)
        self.controller.assert_not_called()

    async def test_binding_replays_existing_intent_without_new_nonce(self):
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        await self.run_bridge(self.args("bind-rollback"))
        self.service.rollback_preflight.assert_not_called()
        self.assertEqual(
            self.b.data["rollback_claim"]["reservation_nonce"],
            B_CLAIM["reservation_nonce"],
        )

    async def test_unknown_release_registration_commit_performs_no_source_mutation(
        self,
    ):
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        self.fail_commit = 3
        with self.assertRaises(OSError):
            await self.run_bridge(self.args("restore-source"))
        self.reads.status.assert_not_called()
        self.assertIn("application_claim", self.a.data)
        self.controller.assert_not_called()

    async def test_restore_commits_responsibility_and_drains_before_native_release(
        self,
    ):
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        await self.run_bridge(self.args("restore-source"))
        self.assertLess(
            self.events.index("save_release_id"), self.events.index("unbind_a")
        )
        self.assertLess(self.events.index("unbind_a"), self.events.index("release_a"))
        self.assertIn(
            "commit",
            self.events[
                self.events.index("save_release_id") : self.events.index("unbind_a")
            ],
        )
        self.close.assert_awaited_once()
        self.service.finish_rollback.assert_awaited_once()
        self.assertTrue(self.b.data["rollback_claim"]["reserved"])

    async def test_unsettled_publication_never_unbinds_or_opens_a(self):
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        self.reads.finish_withdrawal.side_effect = FenceError(
            "read_publication_unsettled"
        )
        with self.assertRaises(FenceError):
            await self.run_bridge(self.args("restore-source"))
        self.assertIn("application_claim", self.a.data)
        self.controller.assert_not_called()
        self.service.finish_rollback.assert_not_called()

    async def test_changed_b_proof_is_rejected_before_any_source_action(self):
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        self.service.bind_rollback.side_effect = FenceError(
            "finalization_native_changed"
        )
        with self.assertRaises(FenceError):
            await self.run_bridge(self.args("restore-source"))
        self.service.begin_restoration.assert_not_called()
        self.reads.status.assert_not_called()
        self.assertIn("application_claim", self.a.data)
        self.controller.assert_not_called()

    async def test_partial_restore_resumes_without_source_claim_and_reuses_saved_uuid(
        self,
    ):
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        self.a.data.pop("application_claim")
        self.group.update(state="rollback_restoring", revision=4)
        self.group["source_release_operation_id"] = str(uuid.uuid4())
        saved = self.group["source_release_operation_id"]
        self.service.status.return_value = deepcopy(self.group)
        self.service.bind_rollback.return_value = deepcopy(self.group)
        self.reads.status.return_value = SimpleNamespace(state="withdrawn", revision=3)
        self.finals.status.return_value = SimpleNamespace(
            state="invalidated", revision=5
        )
        args = self.args("restore-source")
        args.expected_revision = 4
        await self.run_bridge(args)
        self.assertEqual(self.group["source_release_operation_id"], saved)
        self.reads.begin_withdrawal.assert_not_called()
        self.finals.begin_invalidation.assert_not_called()
        self.assertIn("release_a", self.events)

    async def test_foreign_b_claim_and_stale_revision_cannot_begin_restoration(self):
        self.b.data["rollback_claim"] = {
            **B_CLAIM,
            "reservation_nonce": str(uuid.uuid4()),
        }
        with self.assertRaises(FenceError):
            await self.run_bridge(self.args("restore-source"))
        self.service.begin_restoration.assert_not_called()
        self.b.data["rollback_claim"] = deepcopy(B_CLAIM)
        args = self.args("restore-source")
        args.expected_revision = 2
        with self.assertRaises(FenceError):
            await self.run_bridge(args)
        self.service.begin_restoration.assert_not_called()

    async def test_preview_probes_without_transaction_and_rechecks_before_signing(self):
        context = {"id": CUTOVER, "targets": {}}
        self.service.prepare_activation = AsyncMock(return_value=context)
        review = {"context": context, "impact_token": "private"}
        self.service.issue_review = Mock(return_value=review)

        async def probe(session, captured):
            self.assertEqual(self.events[-1], "closed")
            return {"nine": True}

        with patch.object(cli, "probe_targets", side_effect=probe):
            await self.run_bridge(self.args("preview"))
        self.assertEqual(self.service.prepare_activation.await_count, 2)
        self.service.issue_review.assert_called_once_with(
            context, context, {"nine": True}
        )
        self.assertEqual(
            json.loads(self.args("preview").review_file.read_text()), review
        )


if __name__ == "__main__":
    unittest.main()
