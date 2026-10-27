"""Incremental generations preserve immutable physical evidence across reports."""

import hashlib
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_storage_migration_copy import BODY, TARGET, Body, Client, inputs
import storage_migration_copy as core
import copy_storage_migration as cli


class Source(Client):
    def __init__(self):
        super().__init__(source=True)
        self.values = {}

    def response(self, kwargs, *, body=False):
        response = super().response(kwargs, body=body)
        value = self.values.get((kwargs["Key"], kwargs.get("VersionId")), BODY)
        response["ContentLength"] = len(value)
        if body:
            response["Body"].close()
            response["Body"] = Body(value)
            self.bodies.append(response["Body"])
        return response


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aap-copy-generations-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = Source(), Client()
        self.inputs_count = 0

    def files(self, rows, **kwargs):
        self.inputs_count += 1
        directory = self.root / f"inputs-{self.inputs_count}"
        directory.mkdir(mode=0o700)
        return inputs(directory, rows, **kwargs)

    def initial(self, rows):
        path = self.root / "root.sqlite"
        core.plan(path, *self.files(rows), TARGET)
        return path

    def child(self, parent_path, rows, **kwargs):
        paths = self.files(rows, complete=kwargs.pop("complete", True))
        path = self.root / f"child-{self.inputs_count}.sqlite"
        with core.Checkpoint(parent_path) as parent:
            evidence = parent.evidence_digest()
            result = core.plan_generation(
                path,
                *paths,
                TARGET,
                parent=parent,
                parent_evidence_sha256=evidence,
                **kwargs,
            )
            self.assertEqual(parent.evidence_digest(), evidence)
        return path, result

    def run_copy(self, path, **kwargs):
        with core.Checkpoint(path) as state:
            return core.execute(state, self.source, self.target, **kwargs)

    def rows(self, path):
        with core.Checkpoint(path) as state:
            return {
                row["source"]["key"] + ":" + str(row["source"]["version_id"]): row
                for row in state.rows()
            }

    def test_three_generations_exact_receipt_hidden_by_foreign_latest(self):
        rows = [
            {"key": "stable", "is_latest": True},
            {"key": "old-only"},
            {"key": "mutable", "version_id": "null"},
        ]
        root = self.initial(rows)
        self.assertEqual(self.run_copy(root)["counts"], {"verified": 3})
        old = self.rows(root)
        stable = old["stable:v1"]
        self.target.objects[stable["target_key"]].append(
            {"body": b"foreign", "metadata": {}, "version": "foreign-latest"}
        )
        changed = b"changed mutable bytes"
        self.source.values[("mutable", "null")] = changed
        next_rows = [
            {"key": "stable", "is_latest": False},
            {
                "key": "stable",
                "version_id": "deleted",
                "type": "delete_marker",
                "is_latest": True,
            },
            {
                "key": "mutable",
                "version_id": "null",
                "size_bytes": len(changed),
                "verification": {
                    "status": "verified",
                    "sha256": hashlib.sha256(changed).hexdigest(),
                },
            },
            {"key": "new", "version_id": "new-version"},
        ]
        child, result = self.child(root, next_rows, complete=False)
        summary = result["generation_summary"]
        self.assertEqual(
            summary,
            dict(
                reused_objects=1,
                new_objects=1,
                mutable_recopies=1,
                source_identity_conflicts=0,
                previous_only_objects=1,
                delete_markers=1,
                history_records=3,
            ),
        )
        self.assertEqual(result["counts"], {"pending": 3, "skipped": 1})
        current = self.rows(child)
        for key in ("target_key", "nonce", "content", "receipt"):
            self.assertEqual(current["stable:v1"][key], stable[key])
        self.assertFalse(current["stable:v1"]["is_latest"])
        self.assertTrue(current["stable:deleted"]["is_latest"])
        self.source.calls.clear()
        self.assertEqual(self.run_copy(child)["counts"], {"verified": 3, "skipped": 1})
        self.assertEqual(self.target.puts, 5)
        self.assertFalse(any(call["Key"] == "stable" for call in self.source.calls))
        exact = [
            call for call in self.target.calls if call["Key"] == stable["target_key"]
        ]
        self.assertEqual(
            {call.get("VersionId") for call in exact[-3:]},
            {stable["receipt"]["version_id"]},
        )
        third, result = self.child(child, next_rows, complete=False)
        self.assertEqual(result["generation"], 3)
        self.assertEqual(result["generation_summary"]["reused_objects"], 2)
        self.assertEqual(result["generation_summary"]["history_records"], 6)
        with core.Checkpoint(third) as state:
            records = list(state.history())
            physical = [
                r["value"]
                for r in records
                if r["record_type"] == "result" and core.physical_intent(r["value"])
            ]
            self.assertEqual(
                len({row["target_key"] for row in physical}), len(physical)
            )
            self.assertEqual(
                len(
                    [
                        row
                        for row in physical
                        if row["target_key"] == stable["target_key"]
                    ]
                ),
                1,
            )
            self.assertTrue(any(row["source"]["key"] == "old-only" for row in physical))
        self.assertEqual(self.run_copy(third)["counts"], {"verified": 3, "skipped": 1})
        self.assertEqual(self.target.puts, 6)
        fourth, result = self.child(third, next_rows, complete=False)
        self.assertEqual(result["generation_summary"]["history_records"], 7)
        self.assertEqual(
            self.rows(fourth)["stable:v1"]["receipt"]["version_id"],
            stable["receipt"]["version_id"],
        )

    def test_unknown_ack_reuses_nonce_and_merges_stronger_receipt(self):
        root = self.initial([{}])
        self.target.lose_ack = True
        self.assertEqual(self.run_copy(root)["counts"], {"uncertain": 1})
        prior = next(iter(self.rows(root).values()))
        child, result = self.child(root, [{}])
        self.assertEqual(result["generation_summary"]["reused_objects"], 1)
        self.target.lose_ack = False
        self.source.calls.clear()
        self.assertEqual(self.run_copy(child)["counts"], {"verified": 1})
        self.assertEqual(self.target.puts, 1)
        self.assertFalse(self.source.calls)
        third, result = self.child(child, [{}])
        self.assertEqual(result["generation_summary"]["history_records"], 1)
        with core.Checkpoint(third) as state:
            historical = next(state.history())["value"]
            self.assertEqual(historical["nonce"], prior["nonce"])
            self.assertEqual(historical["receipt"]["version_id"], "actual-target-1")

    def test_explicit_identity_conflicts_preserve_old_facts(self):
        for mutation in (
            {"etag": '"different"'},
            {"size_bytes": 4},
            {"verification": {"status": "verified", "sha256": "0" * 64}},
        ):
            with self.subTest(mutation=mutation):
                directory = self.root / f"case-{self.inputs_count}"
                directory.mkdir(mode=0o700)
                root = directory / "root.sqlite"
                core.plan(root, *self.files([{}]), TARGET)
                self.run_copy(root)
                old = next(iter(self.rows(root).values()))
                child, result = self.child(root, [mutation])
                self.assertEqual(
                    result["generation_summary"]["source_identity_conflicts"], 1
                )
                current = next(iter(self.rows(child).values()))
                self.assertEqual(current["decision"], "source_identity_conflict")
                self.assertIsNone(current["target_key"])
                with core.Checkpoint(child) as state:
                    self.assertEqual(
                        next(state.history())["value"]["receipt"], old["receipt"]
                    )

    def test_missing_object_reappears_from_self_contained_history(self):
        root = self.initial([{}])
        self.run_copy(root)
        original = next(iter(self.rows(root).values()))
        child, result = self.child(root, [], complete=False)
        self.assertEqual(result["generation_summary"]["previous_only_objects"], 1)
        root.unlink()
        third, result = self.child(child, [{}])
        self.assertEqual(result["generation_summary"]["reused_objects"], 1)
        self.assertEqual(
            next(iter(self.rows(third).values()))["target_key"], original["target_key"]
        )
        self.assertEqual(self.run_copy(third)["counts"], {"verified": 1})
        self.assertEqual(self.target.puts, 1)

    def test_unwritten_and_unpinned_rows_receive_new_child_identity(self):
        root = self.initial([{}, {"key": "unpinned", "version_id": None}])
        old = self.rows(root)
        child, result = self.child(root, [{}, {"key": "unpinned", "version_id": None}])
        self.assertEqual(result["generation_summary"]["reused_objects"], 0)
        self.assertEqual(result["generation_summary"]["new_objects"], 1)
        self.assertEqual(result["generation_summary"]["mutable_recopies"], 1)
        for key, row in self.rows(child).items():
            self.assertNotEqual(row["target_key"], old[key]["target_key"])

    def test_evidence_digest_is_logical_and_checks_rows_facts_and_history(self):
        root = self.initial([{}, {"type": "issue", "code": "observed"}])
        with core.Checkpoint(root) as state:
            before = state.evidence_digest()
            row = state.db.execute("SELECT id,data FROM results").fetchone()
            value = json.loads(row[1])
            state.db.execute(
                "UPDATE results SET data=? WHERE id=?",
                (json.dumps(value, indent=2), row[0]),
            )
            state.db.commit()
            state.db.execute("VACUUM")
            self.assertEqual(before, state.evidence_digest())
            value["error"] = "changed_observation"
            state.save(value)
            self.assertNotEqual(before, state.evidence_digest())
        child, _ = self.child(root, [{}])
        with core.Checkpoint(child) as state:
            state.db.execute("UPDATE history SET data='{}'")
            with self.assertRaisesRegex(core.CopyError, "checkpoint_corrupt"):
                state.evidence_digest()

    def test_wrong_parent_digest_policy_and_row_budget_are_rejected(self):
        root = self.initial([{}])
        paths = self.files([{}])
        with core.Checkpoint(root) as parent:
            output = self.root / "bad.sqlite"
            with self.assertRaisesRegex(core.CopyError, "parent_evidence_mismatch"):
                core.plan_generation(
                    output,
                    *paths,
                    TARGET,
                    parent=parent,
                    parent_evidence_sha256="0" * 64,
                )
            self.assertFalse(output.exists())
            with self.assertRaisesRegex(core.CopyError, "generation_target_mismatch"):
                core.plan_generation(
                    output,
                    *paths,
                    TARGET | {"key_prefix": "elsewhere/"},
                    parent=parent,
                    parent_evidence_sha256=parent.evidence_digest(),
                )
        with self.assertRaisesRegex(core.CopyError, "plan_row_budget"):
            self.child(root, [{}], max_rows=1)

    def test_creator_and_target_provenance_cannot_be_rewritten(self):
        root = self.initial([{}])
        self.run_copy(root)
        child, _ = self.child(root, [{}])
        with core.Checkpoint(child) as state:
            original = next(state.rows())
            for change in (
                {"target_key": "outside/target"},
                {"creation_plan_uuid": state.meta["plan_uuid"]},
            ):
                with (
                    self.subTest(change=change),
                    self.assertRaisesRegex(core.CopyError, "checkpoint_corrupt"),
                ):
                    state.save(original | change)

    def test_existing_cli_can_status_run_and_verify_child_checkpoint(self):
        root = self.initial([{}])
        self.run_copy(root)
        child, _ = self.child(root, [{}])
        config = self.root / "target.json"
        config.write_text(json.dumps(TARGET))
        config.chmod(0o600)
        self.source.close = self.target.close = lambda: None
        self.source.calls.clear()
        for action, expected in (("status", 1), ("run", 0), ("verify", 0)):
            out, err = io.StringIO(), io.StringIO()
            args = [action, "--checkpoint", str(child)]
            if action != "status":
                args.extend(["--target-config", str(config)])
            with (
                patch.object(cli, "configured_source", return_value=self.source),
                patch.object(cli, "client", return_value=self.target),
                redirect_stdout(out),
                redirect_stderr(err),
            ):
                self.assertEqual(cli.main(args), expected, err.getvalue())
            self.assertEqual(
                json.loads(out.getvalue())["schema"], core.GENERATION_SCHEMA
            )
        self.assertEqual(self.target.puts, 1)
        self.assertFalse(self.source.calls)


if __name__ == "__main__":
    unittest.main()
