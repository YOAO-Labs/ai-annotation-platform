"""Fixture and whole-cycle accounting checks without external resources."""

from pathlib import Path
import tempfile
import unittest

from benchmark_dataset_sync import aggregate_metrics, build_files
from benchmark_dataset_scan import tree_fingerprint


class SyncBenchmarkTests(unittest.TestCase):
    def test_fixture_has_exact_members_and_additions_do_not_replace_old_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            before = build_files(root, 1001)
            original = {path: (root / path).read_bytes() for path in before}
            added = build_files(root, 10, additions=True)
            actual, _ = tree_fingerprint(root)
            self.assertEqual(
                {path for path, kind in actual.items() if kind == "file"},
                set(before) | set(added),
            )
            self.assertTrue(
                all(
                    (root / path).read_bytes() == body
                    for path, body in original.items()
                )
            )
            self.assertTrue(all(Path(path).name.startswith("new-") for path in added))
            self.assertGreater(len({value["size"] for value in before.values()}), 1)
            with self.assertRaises(FileExistsError):
                build_files(root, 1)

    def row(self, start, finish, *, source_bytes, task="registration"):
        return {
            "task_name": task,
            "started_monotonic": start,
            "settled_monotonic": finish,
            "filesystem": {
                "source_bytes": source_bytes,
                "baseline_bytes": source_bytes,
            },
            "sql": {"statements": 2, "seconds": 0.1},
        }

    def test_whole_cycle_sums_all_chunks_and_keeps_outside_task_time(self):
        result = aggregate_metrics(
            [
                ("first", self.row(1, 2, source_bytes=10)),
                ("second", self.row(2.5, 3.5, source_bytes=20)),
                ("last", self.row(4, 4.5, source_bytes=0, task="sync")),
            ],
            5,
        )
        self.assertEqual(result["task_count"], 3)
        self.assertEqual(result["tasks_by_name"], {"registration": 2, "sync": 1})
        self.assertEqual(result["filesystem"]["baseline_bytes"], 30)
        self.assertEqual(result["sql"]["statements"], 6)
        self.assertEqual(result["worker_execution_seconds"], 2.5)
        self.assertEqual(result["outside_task_seconds"], 2.5)

    def test_incomplete_duplicate_or_invalid_observations_cannot_be_success(self):
        row = self.row(1, 2, source_bytes=10)
        for rows, wall in [
            ([], 2),
            ([("same", row), ("same", row)], 3),
            ([("reverse", self.row(2, 1, source_bytes=0))], 3),
            ([("too-long", row)], 0.1),
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                aggregate_metrics(rows, wall)


if __name__ == "__main__":
    unittest.main()
