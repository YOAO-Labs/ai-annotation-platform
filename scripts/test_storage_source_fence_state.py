"""Pure protocol tests: no Docker, database, provider, or shared runtime."""

from copy import deepcopy
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

from storage_source_fence_state import (
    CLAIM_SCHEMA,
    Controller,
    FenceError,
    Journal,
    MAX_BYTES,
    MAX_COUNTER,
    ROLLBACK_CLAIM_SCHEMA,
    ROLLBACK_SCHEMA,
    SCHEMA,
)

DIGEST = "a" * 64
SEAL = str(uuid.UUID(int=1))
RELEASE = str(uuid.UUID(int=2))
NEXT = str(uuid.UUID(int=3))


def application_claim(**updates):
    return {
        "schema": CLAIM_SCHEMA,
        "finalization_id": str(uuid.UUID(int=10)),
        "window_id": str(uuid.UUID(int=11)),
        "expected_window_revision": 3,
        "database_identity": "b" * 64,
        "reservation_nonce": str(uuid.UUID(int=12)),
        "native_operation_id": SEAL,
        "config_digest": DIGEST,
        "reserved": False,
        **updates,
    }


def rollback_claim(**updates):
    return {
        "schema": ROLLBACK_CLAIM_SCHEMA,
        "cutover_id": str(uuid.UUID(int=20)),
        "expected_cutover_revision": 3,
        "database_identity": "b" * 64,
        "reservation_nonce": str(uuid.UUID(int=21)),
        "native_operation_id": SEAL,
        "config_digest": DIGEST,
        "reserved": False,
        **updates,
    }


class Adapter:
    def __init__(self, path):
        self.path = path
        self.calls = []
        self.read_only = False
        self.disabled = False
        self.generation = 0
        self.uploads = 2
        self.fail = None
        self.error = RuntimeError("https://secret.example/private-key?signature=secret")
        self.snapshot = {
            "secret_path": "/private/source-keys",
            "original": {"rules": [{"enabled": True}]},
            "disabled": {"rules": [{"enabled": False}]},
            "counts": {"buckets": 9, "lifecycle_rules": 1},
        }

    def call(self, name, mutate=None, *, intent=None):
        self.calls.append(name)
        if intent is not None:
            saved = json.loads(self.path.read_bytes())
            assert saved["intent"]["step"] == intent
            assert saved["phase"] != "held"
            assert saved["observation"] is None
        if mutate is not None:
            mutate()
        if self.fail == name:
            raise self.error

    def preview(self):
        self.call("preview")
        return {"buckets": 9, "lifecycle_rules": 1}

    def readonly(self):
        self.call(
            "readonly", lambda: setattr(self, "read_only", True), intent="readonly"
        )

    def prepare(self):
        self.call("prepare", intent="prepare")
        assert self.read_only
        return deepcopy(self.snapshot)

    def disable_lifecycle(self, snapshot):
        assert snapshot == self.snapshot
        self.call(
            "disable_lifecycle",
            lambda: setattr(self, "disabled", True),
            intent="disable_lifecycle",
        )

    def barrier(self):
        saved = json.loads(self.path.read_bytes())
        step = saved["intent"]["step"]
        name = "barrier1" if step == "barrier_before_abort" else "barrier2"
        self.call(
            name, lambda: setattr(self, "generation", self.generation + 1), intent=step
        )
        return {"generation": self.generation, "process_exit_confirmed": True}

    def abort_uploads(self):
        count = self.uploads
        self.call(
            "abort_uploads", lambda: setattr(self, "uploads", 0), intent="abort_uploads"
        )
        return count

    def verify(self, snapshot, barriers):
        self.call("verify")
        assert snapshot == self.snapshot
        if not self.read_only or not self.disabled or self.uploads or len(barriers) < 2:
            raise ValueError("not held")
        if barriers[-1]["generation"] != self.generation:
            raise ValueError("process changed")
        return {
            "observed_at": "2026-10-26T06:00:00+00:00",
            "counts": {"buckets": 9, "multipart_uploads": 0},
        }

    def restore_lifecycle(self, snapshot):
        assert snapshot == self.snapshot
        # A CAS failure occurs before restoring anything.
        self.call("restore_lifecycle", intent="restore_lifecycle")
        self.disabled = False

    def writable(self):
        self.call(
            "writable", lambda: setattr(self, "read_only", False), intent="writable"
        )

    def verify_open(self, snapshot):
        self.call("verify_open")
        assert snapshot is None or snapshot == self.snapshot
        if self.read_only or self.disabled:
            raise ValueError("not open")


class Base(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="aap-fence-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "private" / "state.json"

    def create(self):
        return Journal(self.path, DIGEST, create=True)

    def reopen(self):
        return Journal(self.path, DIGEST)

    def assert_code(self, code, call):
        with self.assertRaises(FenceError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)


class JournalTests(Base):
    def test_create_atomic_save_reload_and_private_modes(self):
        with self.create() as journal:
            self.assertEqual(journal.data["revision"], 1)
            self.assertEqual(journal.data["phase"], "open")
            self.assertEqual(journal.data["schema"], SCHEMA)
            self.assertIsNone(journal.data["snapshot"])
            journal.data["test_fact"] = {"value": "保存"}
            journal.save()
            self.assertEqual(journal.data["revision"], 2)
        with self.reopen() as journal:
            self.assertEqual(journal.data["test_fact"], {"value": "保存"})
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        for path in self.path.parent.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(
            {p.name for p in self.path.parent.iterdir()},
            {"state.json", "state.json.lock"},
        )

    def test_save_fsyncs_data_before_replace_and_directory_after(self):
        with self.create() as journal:
            events = []
            fsync, replace = os.fsync, os.replace

            def sync(fd):
                events.append(
                    "directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
                )
                return fsync(fd)

            def rename(*args, **kwargs):
                events.append("replace")
                return replace(*args, **kwargs)

            with (
                patch("storage_source_fence_state.os.fsync", side_effect=sync),
                patch("storage_source_fence_state.os.replace", side_effect=rename),
            ):
                journal.save()
            self.assertEqual(events, ["file", "replace", "directory"])

    def test_failed_atomic_write_preserves_old_data_and_cleans_temporary(self):
        with self.create() as journal:
            before = self.path.read_bytes()
            with patch(
                "storage_source_fence_state.os.replace", side_effect=OSError("secret")
            ):
                self.assert_code("journal_write_failed", journal.save)
            self.assertEqual(self.path.read_bytes(), before)
            self.assertEqual(journal.data["revision"], 1)
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_changed_file_is_not_overwritten(self):
        with self.create() as journal:
            self.path.write_text("{}")
            self.assert_code("journal_changed", journal.save)
            self.assertEqual(self.path.read_text(), "{}")

    def test_second_opener_is_nonblocking_even_in_same_process(self):
        with self.create():
            self.assert_code("journal_busy", lambda: self.reopen().__enter__())
        with self.reopen():
            pass

    def test_nonblocking_lock_excludes_an_independent_process(self):
        script = """
import sys
from pathlib import Path
from storage_source_fence_state import FenceError, Journal
try:
    with Journal(Path(sys.argv[1]), sys.argv[2]):
        raise SystemExit(1)
except FenceError as exc:
    print(exc.code)
    raise SystemExit(23)
"""
        with self.create():
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.path), DIGEST],
                cwd=Path(__file__).parent,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stdout.strip(), "journal_busy")

    def test_create_never_overwrites_an_existing_journal(self):
        with self.create():
            pass
        before = self.path.read_bytes()
        self.assert_code("journal_exists", lambda: self.create().__enter__())
        self.assertEqual(self.path.read_bytes(), before)

    def test_config_digest_is_bound_on_load_and_save(self):
        with self.create() as journal:
            journal.data["config_digest"] = "b" * 64
            self.assert_code("journal_invalid", journal.save)
        self.assert_code(
            "journal_invalid", lambda: Journal(self.path, "b" * 64).__enter__()
        )
        self.assert_code("config_digest_invalid", lambda: Journal(self.path, "secret"))

    def test_parent_and_journal_and_lock_require_private_permissions(self):
        with self.create():
            pass
        for path, wide, private in (
            (self.path.parent, 0o755, 0o700),
            (self.path, 0o644, 0o600),
            (self.path.with_name("state.json.lock"), 0o644, 0o600),
        ):
            with self.subTest(path=path.name):
                path.chmod(wide)
                self.assert_code(
                    "journal_permissions", lambda: self.reopen().__enter__()
                )
                path.chmod(private)

    def test_foreign_owner_is_rejected(self):
        with self.create():
            pass
        original = os.fstat

        def foreign(fd):
            real = original(fd)
            values = list(real)
            values[4] = os.getuid() + 1
            return os.stat_result(values)

        with patch("storage_source_fence_state.os.fstat", side_effect=foreign):
            self.assert_code("journal_permissions", lambda: self.reopen().__enter__())

    def test_symlink_journal_lock_parent_and_ancestor_are_rejected(self):
        with self.create():
            pass
        for path in (self.path, self.path.with_name("state.json.lock")):
            with self.subTest(path=path.name):
                moved = path.with_name(path.name + ".original")
                path.rename(moved)
                path.symlink_to(moved)
                with self.assertRaises(FenceError):
                    with self.reopen():
                        pass
                path.unlink()
                moved.rename(path)
        parent = self.path.parent
        moved = parent.with_name("real-private")
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(FenceError):
            with self.reopen():
                pass
        alias = self.root / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(FenceError):
            with Journal(alias / "real-private" / "state.json", DIGEST):
                pass

    def test_hardlinked_journal_is_rejected(self):
        with self.create():
            pass
        os.link(self.path, self.root / "linked")
        self.assert_code("journal_permissions", lambda: self.reopen().__enter__())

    def test_duplicate_json_keys_nan_and_bad_structure_are_rejected(self):
        with self.create():
            pass
        for value in ('{"revision":1,"revision":2}', '{"a":NaN}', "[]", '{"phase":[]}'):
            with self.subTest(value=value):
                self.path.write_text(value)
                with self.assertRaises(FenceError):
                    with self.reopen():
                        pass

    def test_budget_is_bounded_on_load_and_before_write(self):
        with self.create() as journal:
            before = self.path.read_bytes()
            journal.data["large"] = "a" * MAX_BYTES
            self.assert_code("journal_budget", journal.save)
            self.assertEqual(self.path.read_bytes(), before)
        self.path.write_bytes(b" " * (MAX_BYTES + 1))
        self.assert_code("journal_budget", lambda: self.reopen().__enter__())

    def test_revision_cannot_be_reset_by_a_caller(self):
        with self.create() as journal:
            journal.data["revision"] = 0
            self.assert_code("journal_revision_invalid", journal.save)


class ControllerTests(Base):
    def test_full_order_intent_before_native_and_explicit_release(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            result = controller.seal(1, SEAL)
            self.assertTrue(result["held"])
            self.assertEqual(
                adapter.calls,
                [
                    "preview",
                    "readonly",
                    "prepare",
                    "disable_lifecycle",
                    "barrier1",
                    "abort_uploads",
                    "barrier2",
                    "verify",
                ],
            )
            self.assertEqual(result["counts"]["aborted_uploads"], 2)
            self.assertEqual(journal.data["snapshot"], adapter.snapshot)
            result = controller.release(result["revision"], RELEASE)
            self.assertEqual(
                adapter.calls[-3:], ["restore_lifecycle", "writable", "verify_open"]
            )
            self.assertEqual(result["phase"], "released")
            self.assertFalse(result["held"])
            self.assertFalse(adapter.disabled)
            self.assertFalse(adapter.read_only)

    def test_wrong_cas_and_invalid_uuid_have_no_side_effects(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            before = self.path.read_bytes()
            for call, code in (
                (lambda: controller.seal(0, SEAL), "revision_changed"),
                (lambda: controller.seal(True, SEAL), "revision_changed"),
                (lambda: controller.release(0, RELEASE), "revision_changed"),
                (lambda: controller.seal(1, "not-a-uuid"), "operation_id_invalid"),
                (
                    lambda: controller.release(
                        1, "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
                    ),
                    "operation_id_invalid",
                ),
            ):
                with self.subTest(code=code):
                    self.assert_code(code, call)
            self.assertEqual(adapter.calls, [])
            self.assertEqual(self.path.read_bytes(), before)

    def test_preview_error_has_no_intent_or_native_mutation(self):
        adapter = Adapter(self.path)
        adapter.fail = "preview"
        with self.create() as journal:
            self.assert_code(
                "native_operation_failed",
                lambda: Controller(journal, adapter).seal(1, SEAL),
            )
            self.assertEqual(journal.data["phase"], "open")
            self.assertEqual(journal.data["revision"], 1)
            self.assertEqual(adapter.calls, ["preview"])

    def test_every_native_seal_failure_or_cancel_invalidates_proof_and_can_resume(self):
        for step in (
            "readonly",
            "prepare",
            "disable_lifecycle",
            "barrier1",
            "abort_uploads",
            "barrier2",
            "verify",
        ):
            for kind in (RuntimeError, KeyboardInterrupt, SystemExit):
                with (
                    self.subTest(step=step, kind=kind.__name__),
                    tempfile.TemporaryDirectory(dir=self.root) as directory,
                ):
                    path = Path(directory) / "private" / "state.json"
                    adapter = Adapter(path)
                    adapter.fail, adapter.error = step, kind("private native detail")
                    with Journal(path, DIGEST, create=True) as journal:
                        controller = Controller(journal, adapter)
                        if kind is RuntimeError:
                            self.assert_code(
                                "native_operation_failed",
                                lambda: controller.seal(1, SEAL),
                            )
                        else:
                            with self.assertRaises(kind):
                                controller.seal(1, SEAL)
                        self.assertEqual(journal.data["phase"], "uncertain")
                        self.assertIsNone(journal.data["observation"])
                        saved_snapshot = deepcopy(journal.data["snapshot"])
                        revision = journal.data["revision"]
                        self.assertNotIn("writable", adapter.calls)
                    # Reopening the journal alone must never touch the adapter.
                    calls = list(adapter.calls)
                    adapter.fail = None
                    with Journal(path, DIGEST) as journal:
                        self.assertEqual(adapter.calls, calls)
                        controller = Controller(journal, adapter)
                        self.assertFalse(controller.status()["held"])
                        self.assertTrue(controller.seal(revision, SEAL)["held"])
                        if saved_snapshot is not None:
                            self.assertEqual(journal.data["snapshot"], saved_snapshot)
                            self.assertEqual(adapter.calls.count("prepare"), 1)

    def test_pending_operation_cannot_be_taken_over(self):
        adapter = Adapter(self.path)
        adapter.fail = "barrier1"
        with self.create() as journal:
            controller = Controller(journal, adapter)
            self.assert_code(
                "native_operation_failed", lambda: controller.seal(1, SEAL)
            )
            before = self.path.read_bytes(), list(adapter.calls)
            self.assert_code(
                "operation_conflict",
                lambda: controller.seal(journal.data["revision"], NEXT),
            )
            self.assertEqual((self.path.read_bytes(), adapter.calls), before)

    def test_same_held_operation_only_live_verifies_without_restart(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            initial = controller.seal(1, SEAL)
            adapter.calls.clear()
            result = controller.seal(initial["revision"], SEAL)
            self.assertTrue(result["held"])
            self.assertEqual(result["revision"], initial["revision"])
            self.assertEqual(adapter.calls, ["verify"])

    def test_status_never_uses_saved_held_proof_when_the_native_generation_changed(
        self,
    ):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            controller.seal(1, SEAL)
        adapter.generation += 1
        with self.reopen() as journal:
            controller = Controller(journal, adapter)
            self.assert_code("native_operation_failed", controller.status)
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertIsNone(journal.data["observation"])
        with self.reopen() as journal:
            self.assertFalse(Controller(journal, adapter).status()["held"])
        self.assertNotIn("writable", adapter.calls)

    def test_status_cancel_is_propagated_after_durable_invalidation(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            controller.seal(1, SEAL)
            adapter.fail, adapter.error = "verify", KeyboardInterrupt()
            with self.assertRaises(KeyboardInterrupt):
                controller.status()
            self.assertEqual(json.loads(self.path.read_bytes())["phase"], "uncertain")

    def test_release_lifecycle_cas_failure_keeps_gateway_closed_and_resumes_only_same_release(
        self,
    ):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            controller.seal(1, SEAL)
            adapter.fail = "restore_lifecycle"
            self.assert_code(
                "native_operation_failed",
                lambda: controller.release(journal.data["revision"], RELEASE),
            )
            self.assertTrue(adapter.read_only)
            self.assertTrue(adapter.disabled)
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertNotIn("writable", adapter.calls)
            self.assert_code(
                "operation_conflict",
                lambda: controller.seal(journal.data["revision"], SEAL),
            )
            self.assert_code(
                "operation_conflict",
                lambda: controller.release(journal.data["revision"], NEXT),
            )
            adapter.fail = None
            self.assertEqual(
                controller.release(journal.data["revision"], RELEASE)["phase"],
                "released",
            )

    def test_every_release_boundary_failure_or_cancel_loses_proof_without_auto_reopen(
        self,
    ):
        for step in ("restore_lifecycle", "writable", "verify_open"):
            for kind in (RuntimeError, KeyboardInterrupt):
                with (
                    self.subTest(step=step, kind=kind.__name__),
                    tempfile.TemporaryDirectory(dir=self.root) as directory,
                ):
                    path = Path(directory) / "private" / "state.json"
                    adapter = Adapter(path)
                    with Journal(path, DIGEST, create=True) as journal:
                        controller = Controller(journal, adapter)
                        controller.seal(1, SEAL)
                        adapter.fail, adapter.error = step, kind("secret")
                        with self.assertRaises(
                            FenceError if kind is RuntimeError else kind
                        ):
                            controller.release(journal.data["revision"], RELEASE)
                        self.assertEqual(journal.data["phase"], "uncertain")
                        self.assertIsNone(journal.data["observation"])
                        if step == "restore_lifecycle":
                            self.assertTrue(adapter.read_only)
                    before = list(adapter.calls)
                    with Journal(path, DIGEST) as journal:
                        self.assertFalse(Controller(journal, adapter).status()["held"])
                    self.assertEqual(adapter.calls, before)

    def test_explicit_release_can_reopen_after_readonly_lost_ack_without_a_snapshot(
        self,
    ):
        adapter = Adapter(self.path)
        adapter.fail = "readonly"
        with self.create() as journal:
            controller = Controller(journal, adapter)
            self.assert_code(
                "native_operation_failed", lambda: controller.seal(1, SEAL)
            )
            self.assertTrue(adapter.read_only)
            self.assertIsNone(journal.data["snapshot"])
            adapter.fail = None
            self.assertEqual(
                controller.release(journal.data["revision"], RELEASE)["phase"],
                "released",
            )
            self.assertNotIn("restore_lifecycle", adapter.calls)
            self.assertFalse(adapter.read_only)

    def test_release_is_idempotent_for_its_uuid_and_reseal_needs_a_new_uuid(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            controller.seal(1, SEAL)
            self.assert_code(
                "operation_conflict",
                lambda: controller.release(journal.data["revision"], SEAL),
            )
            released = controller.release(journal.data["revision"], RELEASE)
            adapter.calls.clear()
            self.assertEqual(
                controller.release(released["revision"], RELEASE)["phase"], "released"
            )
            self.assertEqual(adapter.calls, ["verify_open"])
            self.assert_code(
                "operation_conflict",
                lambda: controller.seal(journal.data["revision"], SEAL),
            )
            self.assertTrue(controller.seal(journal.data["revision"], NEXT)["held"])
            self.assertEqual(len(journal.data["barriers"]), 2)

    def test_snapshot_arguments_are_copies_and_cannot_change_original_lifecycle_facts(
        self,
    ):
        adapter = Adapter(self.path)
        original = adapter.disable_lifecycle

        def mutate(snapshot):
            original(snapshot)
            snapshot["original"]["rules"].clear()

        adapter.disable_lifecycle = mutate
        with self.create() as journal:
            controller = Controller(journal, adapter)
            self.assertTrue(controller.seal(1, SEAL)["held"])
            self.assertEqual(journal.data["snapshot"], adapter.snapshot)

    def test_public_summary_and_errors_exclude_private_snapshot_paths_and_exception_text(
        self,
    ):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            preview = controller.preview()
            summary = controller.seal(1, SEAL)
            self.assertEqual(
                set(summary),
                {"schema", "revision", "phase", "held", "observed_at", "counts"},
            )
            rendered = json.dumps([preview, summary])
            for private in (
                "secret",
                "source-keys",
                "original",
                "disabled",
                "signature",
                "operation_id",
                str(self.path),
            ):
                self.assertNotIn(private, rendered)
            adapter.fail = "verify"
            with self.assertRaises(FenceError) as caught:
                controller.status()
            self.assertEqual(str(caught.exception), "native_operation_failed")
            self.assertNotIn("secret", str(caught.exception))

    def test_invalid_count_projection_cannot_start_a_fence(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            for counts in (
                {"url": "https://secret"},
                {"a/b": 1},
                {"a": True},
                {"a": -1},
                {f"a{i}": 1 for i in range(65)},
            ):
                adapter.preview = lambda: counts
                self.assert_code(
                    "observation_invalid", lambda: controller.seal(1, SEAL)
                )
                self.assertEqual(journal.data["revision"], 1)
                self.assertFalse(adapter.read_only)

    def test_oversized_native_receipt_cannot_publish_held_and_keeps_durable_responsibility(
        self,
    ):
        adapter = Adapter(self.path)
        adapter.barrier = lambda: {"detail": "x" * 17000}
        with self.create() as journal:
            controller = Controller(journal, adapter)
            self.assert_code("journal_budget", lambda: controller.seal(1, SEAL))
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertTrue(adapter.read_only)
            self.assertTrue(adapter.disabled)
            self.assertEqual(journal.data["barriers"], [])
            self.assertEqual(json.loads(self.path.read_bytes())["phase"], "uncertain")

    def test_failed_durable_intent_prevents_the_first_native_side_effect(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            with patch.object(
                journal, "save", side_effect=FenceError("journal_write_failed")
            ):
                self.assert_code(
                    "journal_write_failed", lambda: controller.seal(1, SEAL)
                )
            self.assertEqual(adapter.calls, ["preview"])
            self.assertFalse(adapter.read_only)

    def test_process_hard_exit_after_barrier_before_ack_keeps_intent_and_can_resume(
        self,
    ):
        script = """
import os, sys
from pathlib import Path
from test_storage_source_fence_state import Adapter
from storage_source_fence_state import Controller, Journal
class Crash(Adapter):
    def barrier(self):
        super().barrier()
        os._exit(74)
path = Path(sys.argv[1])
with Journal(path, sys.argv[2], create=True) as journal:
    Controller(journal, Crash(path)).seal(1, sys.argv[3])
"""
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.path), DIGEST, SEAL],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 74)
        adapter = Adapter(self.path)
        adapter.read_only, adapter.disabled, adapter.generation = True, True, 1
        with self.reopen() as journal:
            self.assertEqual(journal.data["phase"], "sealing")
            self.assertEqual(journal.data["intent"]["step"], "barrier_before_abort")
            self.assertEqual(journal.data["barriers"], [])
            self.assertIsNotNone(journal.data["snapshot"])
            controller = Controller(journal, adapter)
            self.assertFalse(controller.status()["held"])
            self.assertTrue(controller.seal(journal.data["revision"], SEAL)["held"])
            self.assertNotIn("prepare", adapter.calls)

    def test_failed_preview_invalidates_an_existing_held_proof(self):
        adapter = Adapter(self.path)
        with self.create() as journal:
            controller = Controller(journal, adapter)
            controller.seal(1, SEAL)
            adapter.fail = "preview"
            self.assert_code("native_operation_failed", controller.preview)
            self.assertEqual(json.loads(self.path.read_bytes())["phase"], "uncertain")

    def test_safe_native_code_explains_why_sealing_remains_uncertain(self):
        adapter = Adapter(self.path)
        adapter.fail = "verify"
        adapter.error = FenceError("source_multipart_metadata_remaining")
        with self.create() as journal:
            controller = Controller(journal, adapter)
            self.assert_code(
                "source_multipart_metadata_remaining", lambda: controller.seal(1, SEAL)
            )
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertFalse(controller.status()["held"])

    def test_unsavable_new_fact_does_not_prevent_durable_unknown(self):
        adapter = Adapter(self.path)
        original = Journal.save
        failed = False

        def fail_new_snapshot(journal):
            nonlocal failed
            if not failed and journal.data["snapshot"] is not None:
                failed = True
                journal.data["oversized_fact"] = "x" * MAX_BYTES
            return original(journal)

        with self.create() as journal, patch.object(Journal, "save", fail_new_snapshot):
            controller = Controller(journal, adapter)
            self.assert_code("journal_budget", lambda: controller.seal(1, SEAL))
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertIsNone(journal.data["snapshot"])
            self.assertNotIn("oversized_fact", journal.data)
            self.assertEqual(json.loads(self.path.read_bytes())["phase"], "uncertain")
            self.assertTrue(adapter.read_only)
            self.assertFalse(adapter.disabled)


class ApplicationClaimTests(Base):
    def held(self, journal):
        adapter = Adapter(self.path)
        Controller(journal, adapter).seal(journal.data["revision"], SEAL)
        return adapter

    def test_bind_reserved_reload_and_exact_unbind_are_durable(self):
        claim = application_claim()
        with self.create() as journal:
            self.held(journal)
            before = journal.data["revision"]
            journal.bind_application(claim)
            self.assertEqual(journal.data["revision"], before + 1)
            journal.bind_application(deepcopy(claim))
            self.assertEqual(journal.data["revision"], before + 1)
            journal.mark_application_reserved(
                claim["finalization_id"], claim["reservation_nonce"]
            )
            journal.mark_application_reserved(
                claim["finalization_id"], claim["reservation_nonce"]
            )
            self.assertEqual(journal.data["revision"], before + 2)
            journal.bind_application(claim)
            self.assertTrue(journal.data["application_claim"]["reserved"])
        with self.reopen() as journal:
            self.assertTrue(journal.data["application_claim"]["reserved"])
            before = journal.data["revision"]
            journal.unbind_application(
                claim["finalization_id"], claim["reservation_nonce"]
            )
            self.assertEqual(journal.data["revision"], before + 1)
        with self.reopen() as journal:
            self.assertNotIn("application_claim", journal.data)

    def test_binding_requires_held_and_exact_native_identity(self):
        with self.create() as journal:
            self.assert_code(
                "application_claim_invalid",
                lambda: journal.bind_application(application_claim()),
            )
            self.held(journal)
            for updates in ({"native_operation_id": NEXT}, {"config_digest": "c" * 64}):
                self.assert_code(
                    "application_claim_invalid",
                    lambda: journal.bind_application(application_claim(**updates)),
                )
            journal.data.update(phase="uncertain", observation=None)
            journal.save()
            self.assert_code(
                "application_fence_not_held",
                lambda: journal.bind_application(application_claim()),
            )

    def test_all_immutable_identity_fields_are_checked(self):
        with self.create() as journal:
            self.held(journal)
            journal.bind_application(application_claim())
            for key, value in (
                ("finalization_id", NEXT),
                ("window_id", NEXT),
                ("reservation_nonce", NEXT),
                ("database_identity", "c" * 64),
                ("expected_window_revision", 4),
            ):
                self.assert_code(
                    "application_claim_conflict",
                    lambda: journal.bind_application(application_claim(**{key: value})),
                )

    def test_malformed_claims_do_not_change_durable_journal(self):
        with self.create() as journal:
            self.held(journal)
            saved = self.path.read_bytes()
            invalid = [None, {}, application_claim(extra="private")]
            for key, values in {
                "schema": ["other"],
                "finalization_id": [
                    application_claim()["finalization_id"].upper(),
                    "bad",
                ],
                "window_id": [None],
                "reservation_nonce": ["a" * 64],
                "expected_window_revision": [True, 0, -1, 1.0],
                "database_identity": ["b" * 63, "B" * 64],
                "reserved": [0, None],
            }.items():
                for value in values:
                    candidate = application_claim(**{key: value})
                    if candidate != application_claim():
                        invalid.append(candidate)
            for claim in invalid:
                self.assert_code(
                    "application_claim_invalid", lambda: journal.bind_application(claim)
                )
            self.assertEqual(saved, self.path.read_bytes())

    def test_loaded_claim_is_validated_even_in_uncertainty(self):
        for updates in (
            {"config_digest": "c" * 64},
            {"native_operation_id": NEXT},
            {"reserved": 1},
        ):
            with self.subTest(updates=updates):
                with self.create() as journal:
                    self.held(journal)
                    data = deepcopy(journal.data)
                data.update(
                    phase="uncertain",
                    observation=None,
                    application_claim=application_claim(**updates),
                )
                self.path.write_text(json.dumps(data))
                self.assert_code(
                    "application_claim_invalid", lambda: self.reopen().__enter__()
                )
                self.path.unlink()

    def test_bound_seal_release_call_no_adapter_method_even_with_stale_cas(self):
        with self.create() as journal:
            adapter = self.held(journal)
            journal.bind_application(application_claim())
            controller = Controller(journal, adapter)
            adapter.calls.clear()
            for revision in (1, journal.data["revision"]):
                self.assert_code(
                    "application_fence_bound", lambda: controller.seal(revision, SEAL)
                )
                self.assert_code(
                    "application_fence_bound",
                    lambda: controller.release(revision, RELEASE),
                )
            self.assertEqual(adapter.calls, [])

    def test_status_can_invalidate_proof_without_losing_claim(self):
        with self.create() as journal:
            adapter = self.held(journal)
            journal.bind_application(application_claim())
            controller = Controller(journal, adapter)
            summary = controller.status()
            self.assertTrue(summary["held"])
            self.assertNotIn("application_claim", summary)
            adapter.fail = "verify"
            self.assert_code("native_operation_failed", controller.status)
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertEqual(journal.data["application_claim"], application_claim())
            self.assert_code(
                "application_fence_bound",
                lambda: controller.release(journal.data["revision"], RELEASE),
            )
        with self.reopen() as journal:
            self.assertEqual(journal.data["application_claim"], application_claim())

    def test_wrong_nonce_or_finalization_cannot_change_claim(self):
        with self.create() as journal:
            self.held(journal)
            journal.bind_application(application_claim())
            for method in (
                journal.unbind_application,
                journal.mark_application_reserved,
            ):
                for ids in (
                    (NEXT, application_claim()["reservation_nonce"]),
                    (application_claim()["finalization_id"], NEXT),
                ):
                    self.assert_code("application_claim_conflict", lambda: method(*ids))
            self.assertFalse(journal.data["application_claim"]["reserved"])

    def test_failed_unbind_save_keeps_in_memory_and_disk_fence(self):
        with self.create() as journal:
            adapter = self.held(journal)
            claim = application_claim()
            journal.bind_application(claim)
            with patch.object(journal, "_write", side_effect=OSError("private")):
                self.assert_code(
                    "journal_write_failed",
                    lambda: journal.unbind_application(
                        claim["finalization_id"], claim["reservation_nonce"]
                    ),
                )
            self.assertEqual(journal.data["application_claim"], claim)
            self.assert_code(
                "application_fence_bound",
                lambda: Controller(journal, adapter).release(
                    journal.data["revision"], RELEASE
                ),
            )
        with self.reopen() as journal:
            self.assertEqual(journal.data["application_claim"], claim)

    def test_failed_bind_save_is_fail_closed_without_native_calls(self):
        with self.create() as journal:
            adapter = self.held(journal)
            with patch.object(journal, "_write", side_effect=OSError("private")):
                self.assert_code(
                    "journal_write_failed",
                    lambda: journal.bind_application(application_claim()),
                )
            self.assert_code(
                "application_fence_bound",
                lambda: Controller(journal, adapter).seal(
                    journal.data["revision"], SEAL
                ),
            )

    def test_bound_journal_retains_existing_cross_process_lock(self):
        with self.create() as journal:
            self.held(journal)
            journal.bind_application(application_claim())
            code = (
                "from pathlib import Path; from storage_source_fence_state import Journal,FenceError; "
                f"j=Journal(Path({str(self.path)!r}),{DIGEST!r}); "
                "j.__enter__()"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                cwd=Path(__file__).parent,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"journal_busy", result.stderr)
            self.assertNotIn(
                application_claim()["reservation_nonce"].encode(), result.stderr
            )


class RollbackClaimTests(Base):
    def held(self, journal):
        adapter = Adapter(self.path)
        Controller(journal, adapter).seal(journal.data["revision"], SEAL)
        return adapter

    def test_durable_bind_reserve_reload_and_unbind_keep_v2(self):
        claim = rollback_claim()
        with self.create() as journal:
            self.held(journal)
            before = journal.data["revision"]
            journal.bind_rollback(claim)
            self.assertEqual(journal.data["schema"], ROLLBACK_SCHEMA)
            self.assertNotEqual(journal.data["schema"], SCHEMA)
            self.assertEqual(journal.data["revision"], before + 1)
            journal.bind_rollback(deepcopy(claim))
            self.assertEqual(journal.data["revision"], before + 1)
            journal.mark_rollback_reserved(
                claim["cutover_id"], claim["reservation_nonce"]
            )
            journal.mark_rollback_reserved(
                claim["cutover_id"], claim["reservation_nonce"]
            )
            journal.bind_rollback(claim)
            self.assertEqual(journal.data["revision"], before + 2)
        with self.reopen() as journal:
            self.assertTrue(journal.data["rollback_claim"]["reserved"])
            journal.unbind_rollback(claim["cutover_id"], claim["reservation_nonce"])
        with self.reopen() as journal:
            self.assertNotIn("rollback_claim", journal.data)
            self.assertEqual(journal.data["schema"], ROLLBACK_SCHEMA)

    def test_bind_requires_held_and_exact_native_identity(self):
        with self.create() as journal:
            self.assert_code(
                "rollback_claim_invalid",
                lambda: journal.bind_rollback(rollback_claim()),
            )
            self.held(journal)
            for updates in ({"native_operation_id": NEXT}, {"config_digest": "c" * 64}):
                self.assert_code(
                    "rollback_claim_invalid",
                    lambda: journal.bind_rollback(rollback_claim(**updates)),
                )
            journal.data.update(phase="uncertain", observation=None)
            journal.save()
            self.assert_code(
                "rollback_fence_not_held",
                lambda: journal.bind_rollback(rollback_claim()),
            )

    def test_application_and_rollback_claims_are_mutually_exclusive(self):
        with self.create() as journal:
            self.held(journal)
            journal.bind_application(application_claim())
            before = self.path.read_bytes()
            self.assert_code(
                "application_fence_bound",
                lambda: journal.bind_rollback(rollback_claim()),
            )
            self.assertEqual(before, self.path.read_bytes())
            journal.unbind_application(
                application_claim()["finalization_id"],
                application_claim()["reservation_nonce"],
            )
            journal.bind_rollback(rollback_claim())
            before = self.path.read_bytes()
            self.assert_code(
                "rollback_fence_bound",
                lambda: journal.bind_application(application_claim()),
            )
            self.assertEqual(before, self.path.read_bytes())

    def test_every_immutable_group_identity_is_checked(self):
        with self.create() as journal:
            self.held(journal)
            journal.bind_rollback(rollback_claim())
            for key, value in (
                ("cutover_id", NEXT),
                ("expected_cutover_revision", 4),
                ("database_identity", "c" * 64),
                ("reservation_nonce", NEXT),
            ):
                self.assert_code(
                    "rollback_claim_conflict",
                    lambda: journal.bind_rollback(rollback_claim(**{key: value})),
                )

    def test_malformed_or_finalization_claim_cannot_mutate_journal(self):
        with self.create() as journal:
            self.held(journal)
            before = self.path.read_bytes()
            invalid = [None, {}, application_claim(), rollback_claim(extra="private")]
            for key, values in {
                "schema": [CLAIM_SCHEMA, "other"],
                "cutover_id": [str(uuid.UUID(int=0xABCDEF)).upper(), "bad", None],
                "reservation_nonce": ["a" * 64],
                "expected_cutover_revision": [True, 0, -1, 1.0, MAX_COUNTER + 1],
                "database_identity": ["b" * 63, "B" * 64],
                "reserved": [0, None],
            }.items():
                invalid.extend(rollback_claim(**{key: value}) for value in values)
            for claim in invalid:
                if claim == rollback_claim():
                    continue
                self.assert_code(
                    "rollback_claim_invalid", lambda: journal.bind_rollback(claim)
                )
            self.assertEqual(before, self.path.read_bytes())
            self.assertEqual(journal.data["schema"], SCHEMA)

    def test_loading_rejects_v1_rollback_both_claims_and_invalid_uncertain_claim(self):
        with self.create() as journal:
            self.held(journal)
            saved = deepcopy(journal.data)
        for updates, code in (
            ({"rollback_claim": rollback_claim()}, "journal_invalid"),
            (
                {
                    "schema": ROLLBACK_SCHEMA,
                    "rollback_claim": rollback_claim(),
                    "application_claim": application_claim(),
                },
                "journal_invalid",
            ),
            (
                {
                    "schema": ROLLBACK_SCHEMA,
                    "phase": "uncertain",
                    "observation": None,
                    "rollback_claim": rollback_claim(native_operation_id=NEXT),
                },
                "rollback_claim_invalid",
            ),
        ):
            with self.subTest(updates=updates):
                self.path.write_text(json.dumps({**saved, **updates}))
                self.assert_code(code, lambda: self.reopen().__enter__())

    def test_bound_seal_release_fail_before_cas_or_any_native_call(self):
        with self.create() as journal:
            adapter = self.held(journal)
            journal.bind_rollback(rollback_claim())
            adapter.calls.clear()
            controller = Controller(journal, adapter)
            for revision in (1, journal.data["revision"]):
                self.assert_code(
                    "rollback_fence_bound", lambda: controller.seal(revision, "invalid")
                )
                self.assert_code(
                    "rollback_fence_bound",
                    lambda: controller.release(revision, "invalid"),
                )
            self.assertEqual(adapter.calls, [])

    def test_ordinary_cli_cannot_release_or_reseal_a_bound_rollback(self):
        import storage_source_fence as cli

        self.path = self.path.with_name("fence-state.json")
        with self.create() as journal:
            adapter = self.held(journal)
            journal.bind_rollback(rollback_claim())
            revision = journal.data["revision"]
        adapter.directory = self.path.parent / "gateway"
        adapter.calls.clear()
        for command in ("seal", "release"):
            output, error = io.StringIO(), io.StringIO()
            with (
                patch.object(cli, "descriptor", return_value=({}, DIGEST)),
                patch.object(cli, "DockerAdapter", return_value=adapter),
                contextlib.redirect_stdout(output),
                contextlib.redirect_stderr(error),
            ):
                result = cli.main(
                    [
                        command,
                        "--config",
                        "private-config",
                        "--state",
                        str(self.path),
                        "--expected-revision",
                        str(revision),
                        "--operation-id",
                        RELEASE,
                    ]
                )
            self.assertEqual(result, 2)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(
                json.loads(error.getvalue())["code"], "rollback_fence_bound"
            )
            self.assertNotIn(rollback_claim()["reservation_nonce"], error.getvalue())
        self.assertEqual(adapter.calls, [])

    def test_status_failure_retains_claim_after_reload_without_exposing_owner(self):
        with self.create() as journal:
            adapter = self.held(journal)
            journal.bind_rollback(rollback_claim())
            controller = Controller(journal, adapter)
            result = controller.status()
            self.assertTrue(result["held"])
            self.assertEqual(result["schema"], ROLLBACK_SCHEMA)
            self.assertNotIn("rollback_claim", result)
            self.assertNotIn(rollback_claim()["reservation_nonce"], json.dumps(result))
            adapter.fail = "verify"
            self.assert_code("native_operation_failed", controller.status)
        with self.reopen() as journal:
            self.assertEqual(journal.data["phase"], "uncertain")
            self.assertEqual(journal.data["rollback_claim"], rollback_claim())
            self.assert_code(
                "rollback_fence_bound",
                lambda: Controller(journal, adapter).release(
                    journal.data["revision"], RELEASE
                ),
            )

    def test_wrong_owner_or_nonce_cannot_reserve_or_unbind(self):
        with self.create() as journal:
            self.held(journal)
            claim = rollback_claim()
            journal.bind_rollback(claim)
            for method in (journal.mark_rollback_reserved, journal.unbind_rollback):
                for ids in (
                    (NEXT, claim["reservation_nonce"]),
                    (claim["cutover_id"], NEXT),
                ):
                    self.assert_code("rollback_claim_conflict", lambda: method(*ids))
                self.assert_code(
                    "rollback_claim_invalid",
                    lambda: method("bad", claim["reservation_nonce"]),
                )

    def test_failed_bind_and_reserve_retry_must_fsync_before_acknowledging(self):
        with self.create() as journal:
            self.held(journal)
            claim = rollback_claim()
            with patch.object(journal, "_write", side_effect=OSError("private")):
                for _ in range(2):
                    self.assert_code(
                        "journal_write_failed", lambda: journal.bind_rollback(claim)
                    )
            journal.bind_rollback(claim)
            self.assertEqual(
                json.loads(self.path.read_bytes())["rollback_claim"], claim
            )
            with patch.object(journal, "_write", side_effect=OSError("private")):
                for _ in range(2):
                    self.assert_code(
                        "journal_write_failed",
                        lambda: journal.mark_rollback_reserved(
                            claim["cutover_id"], claim["reservation_nonce"]
                        ),
                    )
            journal.mark_rollback_reserved(
                claim["cutover_id"], claim["reservation_nonce"]
            )
            self.assertTrue(
                json.loads(self.path.read_bytes())["rollback_claim"]["reserved"]
            )

    def test_unacknowledged_bind_survives_status_invalidation(self):
        with self.create() as journal:
            adapter = self.held(journal)
            with patch.object(journal, "_write", side_effect=OSError("private")):
                self.assert_code(
                    "journal_write_failed",
                    lambda: journal.bind_rollback(rollback_claim()),
                )
            adapter.fail = "verify"
            controller = Controller(journal, adapter)
            self.assert_code("native_operation_failed", controller.status)
            self.assert_code(
                "rollback_fence_bound",
                lambda: controller.release(journal.data["revision"], RELEASE),
            )
        with self.reopen() as journal:
            self.assertEqual(journal.data["rollback_claim"], rollback_claim())
            self.assertEqual(journal.data["phase"], "uncertain")

    def test_failed_unbind_retains_memory_and_disk_interlock(self):
        with self.create() as journal:
            adapter = self.held(journal)
            claim = rollback_claim()
            journal.bind_rollback(claim)
            with patch.object(journal, "_write", side_effect=OSError("private")):
                self.assert_code(
                    "journal_write_failed",
                    lambda: journal.unbind_rollback(
                        claim["cutover_id"], claim["reservation_nonce"]
                    ),
                )
            self.assertEqual(journal.data["rollback_claim"], claim)
            self.assertEqual(
                json.loads(self.path.read_bytes())["rollback_claim"], claim
            )
            self.assert_code(
                "rollback_fence_bound",
                lambda: Controller(journal, adapter).release(
                    journal.data["revision"], RELEASE
                ),
            )

    def test_failed_fsync_after_rename_is_unknown_and_reloads_claim(self):
        with self.create() as journal:
            adapter = self.held(journal)
            original = os.fsync

            def fail_directory(descriptor):
                if descriptor == journal._directory:
                    raise OSError("private")
                return original(descriptor)

            with patch(
                "storage_source_fence_state.os.fsync", side_effect=fail_directory
            ):
                self.assert_code(
                    "journal_write_failed",
                    lambda: journal.bind_rollback(rollback_claim()),
                )
            self.assert_code(
                "rollback_fence_bound",
                lambda: Controller(journal, adapter).release(
                    journal.data["revision"], RELEASE
                ),
            )
            self.assert_code(
                "journal_changed", lambda: journal.bind_rollback(rollback_claim())
            )
        with self.reopen() as journal:
            self.assertEqual(journal.data["rollback_claim"], rollback_claim())

    def test_cancellation_during_unbind_keeps_interlock(self):
        with self.create() as journal:
            adapter = self.held(journal)
            claim = rollback_claim()
            journal.bind_rollback(claim)
            with patch.object(journal, "_write", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    journal.unbind_rollback(
                        claim["cutover_id"], claim["reservation_nonce"]
                    )
            self.assert_code(
                "rollback_fence_bound",
                lambda: Controller(journal, adapter).release(
                    journal.data["revision"], RELEASE
                ),
            )

    def test_exact_unbind_allows_existing_native_release_without_schema_downgrade(self):
        with self.create() as journal:
            adapter = self.held(journal)
            claim = rollback_claim()
            journal.bind_rollback(claim)
            journal.mark_rollback_reserved(
                claim["cutover_id"], claim["reservation_nonce"]
            )
            journal.unbind_rollback(claim["cutover_id"], claim["reservation_nonce"])
            result = Controller(journal, adapter).release(
                journal.data["revision"], RELEASE
            )
            self.assertEqual(result["phase"], "released")
            self.assertEqual(result["schema"], ROLLBACK_SCHEMA)
            self.assertFalse(adapter.read_only)


if __name__ == "__main__":
    unittest.main()
