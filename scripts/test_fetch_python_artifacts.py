"""Verify the platform bootstrap rejects unusable or untrusted artifact inputs."""

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location(
    "fetch_python_artifacts", Path(__file__).with_name("fetch-python-artifacts.py")
)
ARTIFACTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARTIFACTS)


class ArtifactIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = self.root / "manifest.json"
        self.output = self.root / "staged"

    def manifest_digest(self, entries):
        data = json.dumps({"files": entries}).encode()
        self.manifest.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    def wheel_entry(self, name, data):
        (self.root / name).write_bytes(data)
        return {"file": name, "sha256": hashlib.sha256(data).hexdigest()}

    def test_changed_manifest_is_rejected_before_staging(self):
        entry = self.wheel_entry("yoao_runtime-0.1.0-py3-none-any.whl", b"original")
        digest = self.manifest_digest([entry])
        self.manifest.write_bytes(self.manifest.read_bytes() + b" ")
        with self.assertRaises(ValueError):
            ARTIFACTS.fetch(str(self.manifest), digest, self.output)
        self.assertFalse(self.output.exists())

    def test_bad_selected_wheel_does_not_partially_replace_existing_inputs(self):
        first = self.wheel_entry("yoao_runtime-0.1.0-py3-none-any.whl", b"new runtime")
        second = self.wheel_entry("yoao_contract-0.1.0-py3-none-any.whl", b"contract")
        digest = self.manifest_digest([first, second])
        (self.root / second["file"]).write_bytes(b"tampered contract")
        self.output.mkdir()
        existing = self.output / first["file"]
        existing.write_bytes(b"previous approved runtime")
        with self.assertRaises(ValueError):
            ARTIFACTS.fetch(str(self.manifest), digest, self.output)
        self.assertEqual(existing.read_bytes(), b"previous approved runtime")
        self.assertFalse((self.output / second["file"]).exists())

    def test_unsafe_ambiguous_or_missing_selection_never_stages(self):
        entry = self.wheel_entry("yoao_runtime-0.1.0-py3-none-any.whl", b"runtime")
        cases = [
            ([{**entry, "file": "../" + entry["file"]}], ()),
            ([{**entry, "file": "..\\" + entry["file"]}], ()),
            ([entry, {**entry, "file": "yoao_runtime-0.2.0-py3-none-any.whl"}], ()),
            ([entry], ("yoao-missing-contract",)),
        ]
        for entries, selected in cases:
            with self.subTest(entries=entries, selected=selected):
                digest = self.manifest_digest(entries)
                with self.assertRaises(ValueError):
                    ARTIFACTS.fetch(str(self.manifest), digest, self.output, selected)
                self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
