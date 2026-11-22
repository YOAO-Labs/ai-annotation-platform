import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from check_comments import ADOPTION_AUDIT, comment_only_lines, scan

CHECK = Path(__file__).with_name("check_comments.py").resolve()


class CommentRatchetTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git("init", "-q")
        self.git("config", "core.hooksPath", os.devnull)
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.history = self.root / "apps/api/alembic/versions"
        self.history.mkdir(parents=True)
        self.source = self.history / "before.py"
        self.source.write_text("# existing\nvalue = 1\n")
        self.git("add", self.source.relative_to(self.root))
        self.git("commit", "-qm", "baseline")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root)

    def check(self, *args, ci_base=None):
        env = {k: v for k, v in os.environ.items() if k != "COMMENT_LINT_BASE"}
        if ci_base:
            env["COMMENT_LINT_BASE"] = ci_base
        return subprocess.run(
            [sys.executable, CHECK, *args],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_index_is_independent_of_working_tree_and_counts_duplicates(self):
        new = self.history / "after.py"
        self.git("mv", self.source.relative_to(self.root), new.relative_to(self.root))
        new.write_text("# existing\nvalue = 1\n# existing\n")
        self.git("add", new.relative_to(self.root))
        new.write_text('value = "# existing"\n')
        staged = self.check("--staged")
        self.assertEqual(staged.returncode, 1)
        self.assertIn("after.py:3:", staged.stderr)
        self.assertNotIn("after.py:1:", staged.stderr)
        self.assertEqual(self.check().returncode, 0)

    def test_replacement_and_untracked_file_cannot_spend_deleted_comment(self):
        self.source.write_text('"""# not a comment"""\n# replaced\n')
        (self.root / "other.py").write_text("# existing\n")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn("before.py:2:", result.stderr)
        self.assertIn("other.py:1:", result.stderr)
        self.assertNotIn("before.py:1:", result.stderr)

    def test_deletion_ratchets_at_the_next_base(self):
        self.source.write_text("value = 1\n")
        self.git("commit", "-qam", "remove explanatory comment")
        self.source.write_text("# existing\nvalue = 1\n")
        self.assertEqual(self.check("--base", "HEAD^").returncode, 0)
        result = self.check("--base", "HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn("before.py:1:", result.stderr)

    def test_first_ci_run_preserves_exact_allowances_without_adoption_history(self):
        census = self.root / ADOPTION_AUDIT
        census.parent.mkdir(parents=True)
        existing = hashlib.blake2s(b"# existing", digest_size=16).hexdigest()
        census.write_text(
            "path\tclassifications\tbootstrap_fingerprints\n"
            f"{self.source.relative_to(self.root)}\tR1\t{existing}\n"
        )
        self.assertEqual(self.check(ci_base="HEAD").returncode, 0)
        self.source.write_text("# replacement\nvalue = 1\n")
        result = self.check(ci_base="HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn("before.py:1:", result.stderr)

    def test_live_code_has_no_legacy_budget_including_renames_from_history(self):
        self.git("mv", self.source.relative_to(self.root), "active.py")
        self.assertIn("active.py:1:", self.check("--staged").stderr)

    def test_empty_catch_budget_does_not_allow_a_second_catch_or_ordinary_prose(self):
        target = self.root / "apps/web/src/test.ts"
        target.parent.mkdir(parents=True)
        statement = "try { read(); } catch { /* leave previous value */ }\n"
        target.write_text(statement)
        self.assertEqual(self.check().returncode, 1)
        self.git("add", target.relative_to(self.root))
        self.git("commit", "-qm", "existing lint input")
        target.write_text(statement * 2 + "/* leave previous value */\n")
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("test.ts:1:", result.stderr)
        self.assertIn("test.ts:2:", result.stderr)
        self.assertIn("test.ts:3:", result.stderr)

    def test_workflow_bash_protects_heredoc_data_and_non_shell_scalars(self):
        source = b"""jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: |
          cat <<'TEXT'
          # payload
          TEXT
          echo ok # expense
          # shellcheck disable=SC2086
      - shell: python
        run: |
          value = '# data'
      - uses: example/action
        with:
          run: |
            # not a shell step
"""
        tokens, _ = scan(".github/workflows/check.yml", "yaml", source)
        self.assertEqual(
            [(source[t.start : t.end], t.protected) for t in tokens],
            [(b"# expense", None), (b"# shellcheck disable=SC2086", "shell-tool")],
        )

    def test_mentions_of_a_linter_are_not_directives(self):
        source = (
            b"// eslint should fix this later\n/* eslint quotes: [2, 'double'] */\n"
        )
        tokens, _ = scan("a.js", "javascript", source)
        self.assertEqual([t.protected for t in tokens], [None, "js-tool"])

    def test_line_savings_do_not_count_inline_tokens_twice_or_touch_code(self):
        source = b"/** reason\n * one\n */ const x = 1; // inline\n/* a */ /* b */\n"
        tokens, starts = scan("file.ts", "typescript", source)
        self.assertEqual(comment_only_lines(source, tokens, starts), 3)

    def test_tool_inputs_are_not_removal_candidates(self):
        for path, lang, source, expected in (
            (
                "script.py",
                "python",
                b"#!/usr/bin/env python3\n# note\nx = 1  # type: ignore\n",
                ["shebang", None, "python-tool"],
            ),
            (
                "sample.sql",
                "sql",
                b"-- aap-statement-break\n-- other\nSELECT 1;",
                ["sql-directive", None],
            ),
            (
                ".env.example",
                "env",
                b"# timeout for the generated reference\nTIMEOUT=4\n",
                ["generated-doc-input"],
            ),
        ):
            with self.subTest(path=path):
                tokens, _ = scan(path, lang, source)
                self.assertEqual([t.protected for t in tokens], expected)


if __name__ == "__main__":
    unittest.main()
