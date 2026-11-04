"""Exercise API submodule bootstrap against disposable local Git repositories."""

from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest


class ApiVendorBootstrapTests(unittest.TestCase):
    def test_initializes_missing_submodule_without_overwriting_local_changes(self):
        with tempfile.TemporaryDirectory(prefix="api-vendor-test-") as temporary:
            base = Path(temporary)
            upstream = base / "upstream"
            checkout = base / "checkout"
            upstream.mkdir()
            checkout.mkdir()
            env = {**os.environ, "GIT_ALLOW_PROTOCOL": "file"}

            def git(root, *args):
                return subprocess.run(
                    ["git", "-C", str(root), *args],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            def commit(root):
                git(root, "add", ".")
                git(
                    root,
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "fixture",
                )

            git(upstream, "init")
            (upstream / "trackeval_metrics").mkdir()
            (upstream / "trackeval_metrics/metrics.py").write_text("original\n")
            commit(upstream)
            git(checkout, "init")
            git(
                checkout,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                str(upstream),
                "apps/api/app/vendor/TrackEval",
            )
            (checkout / "scripts").mkdir()
            script = checkout / "scripts/ensure-api-vendor.sh"
            shutil.copy2(Path(__file__).with_name(script.name), script)
            commit(checkout)
            git(
                checkout,
                "submodule",
                "deinit",
                "-f",
                "--",
                "apps/api/app/vendor/TrackEval",
            )

            def bootstrap():
                return subprocess.run(
                    ["bash", str(script)],
                    cwd=checkout,
                    env=env,
                    capture_output=True,
                    text=True,
                )

            result = bootstrap()
            self.assertEqual(result.returncode, 0, result.stderr)
            marker = (
                checkout / "apps/api/app/vendor/TrackEval/trackeval_metrics/metrics.py"
            )
            self.assertEqual(marker.read_text(), "original\n")
            marker.write_text("local change\n")
            self.assertEqual(bootstrap().returncode, 0)
            self.assertEqual(marker.read_text(), "local change\n")
            marker.unlink()
            result = bootstrap()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("TrackEval submodule is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
