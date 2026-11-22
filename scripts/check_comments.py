# /// script
# requires-python = "==3.12.*"
# dependencies = [
#   "tree-sitter==0.25.2", "tree-sitter-language-pack==0.13.0",
#   "tree-sitter-c-sharp==0.24.0", "tree-sitter-embedded-template==0.25.0",
#   "tree-sitter-yaml==0.7.2",
# ]
# ///

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from bisect import bisect_right
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from comment_tokens import language, policy_spans

ADOPTION_AUDIT = Path("docs/research/data/comment-audit-e8a4f595.tsv")
REGULAR = {b"100644", b"100755"}
METADATA = re.compile(rb"^# /// script\r?\n(?:#(?: [^\n]*|\r?)\n)*?# ///\r?$", re.M)
FROZEN = re.compile(
    r"^apps/(?:api/alembic/(?:sql|versions)/|.+/sql/\d{4}[_-]"
    r"|assets-source/src/yoao_assets_source/0453_import_original_verification\.sql$)"
)
LICENSE = re.compile(
    rb"(?:^|\n)\s*(?://+|/\*+!?|\*|\#|--|<!--)\s*"
    rb"(?:SPDX-(?:License-Identifier|FileCopyrightText):|Copyright\s+(?:\(c\)\s*)?\d{4}\b|@license\b)",
    re.I,
)
JS_LANGS = {"javascript", "typescript", "tsx", "html", "vue"}
PY_DIRECTIVE = re.compile(
    rb"#\s*(?:noqa(?=\s*:|\s*$)|(?:ruff|flake8|pyright|mypy|pylint|type|isort|fmt|doctest):"
    rb"|pragma:\s*no (?:cover|branch)\b)"
)
JS_DIRECTIVE = re.compile(
    rb"(?:/{2}|/\*+)[ \t]*(?:@ts-(?:check|nocheck|ignore|expect-error)\b"
    rb"|eslint-(?:env|disable(?:-next-line|-line)?|enable)\b|prettier-ignore\b"
    rb"|(?:istanbul|c8|v8) ignore\b|[@#]__PURE__\b|@vite-ignore\b|[#@]\s*source(?:Mapping)?URL=)"
    rb"|///[ \t]*<reference\s+(?:path|types|lib|no-default-lib)="
    rb"|/\*\s*eslint\s+[\"'@\w/-]+[\"']?\s*:"
)
JSDOC = re.compile(
    rb"(?:^/\*\*|\n\s*\*)\s*@(type|param|returns?|template|typedef|deprecated|satisfies)\b"
)


@dataclass(frozen=True, slots=True)
class Token:
    start: int
    end: int
    first: int
    last: int
    protected: str | None


def protection(path, lang, raw, start, line, metadata):
    if path == ".env.example":
        return "generated-doc-input"
    if path == ".gitignore" and re.fullmatch(
        rb"# (?:>>>|<<<) orca-dag planning artifacts \(managed\)", raw
    ):
        return "managed-ignore-boundary"
    if start == 0 and raw.startswith(b"#!/"):
        return "shebang"
    if LICENSE.search(raw):
        return "legal"
    if lang == "svg" and re.fullmatch(
        rb"<!--\s*(?:svg-source:excalidraw|payload-(?:start|end)|payload-(?:type|version):[\w.+/-]+)\s*-->",
        raw,
    ):
        return "excalidraw-payload"
    if lang == "python":
        if metadata and metadata[0] <= start < metadata[1]:
            return "script-metadata"
        if line <= 2 and re.match(rb"#.*?coding[:=][ \t]*[-\w.]+", raw):
            return "encoding"
        if PY_DIRECTIVE.match(raw):
            return "python-tool"
    if lang in JS_LANGS:
        if JS_DIRECTIVE.match(raw) or (raw.startswith(b"/**") and JSDOC.search(raw)):
            return "js-tool"
    if lang in {"vue", "css", "scss", "html"} and re.match(
        rb"/\*\s*(?:stylelint-(?:disable|enable)\b|prettier-ignore\b)", raw
    ):
        return "css-tool"
    if lang == "sql" and (
        raw.strip() == b"-- aap-statement-break" or raw.startswith(b"/*+")
    ):
        return "sql-directive"
    if lang == "gomod" and raw.strip() == b"// indirect":
        return "go-directive"
    if lang in {"go", "gomod"} and re.match(
        rb"(?://(?:go:|line | \+build |nolint\b)|/\*line )", raw
    ):
        return "go-directive"
    if lang == "dockerfile" and re.match(
        rb"#\s*(?:syntax|escape|check)\s*=", raw, re.I
    ):
        return "docker-directive"
    if lang == "bash" and re.match(rb"#\s*(?:shellcheck\s+\w+=|shfmt:)", raw):
        return "shell-tool"
    if lang == "yaml" and re.match(rb"#\s*(?:yamllint |yaml-language-server:)", raw):
        return "yaml-tool"
    return None


def scan(path, lang, source):
    spans = list(policy_spans(path, lang, source))
    if not spans:
        return [], []
    starts = [0, *(m.end() for m in re.finditer(b"\n", source))]
    header = (
        METADATA.search(source)
        if lang == "python" and b"# /// script" in source
        else None
    )
    metadata = header.span() if header else None
    result = []
    for a, z, context in spans:
        first = bisect_right(starts, a)
        embedded = context == "workflow-bash"
        reason = protection(
            path, "bash" if embedded else lang, source[a:z], a, first, metadata
        )
        if reason is None and not embedded:
            reason = context
        result.append(Token(a, z, first, bisect_right(starts, z - 1), reason))
    return result, starts


def comment_only_lines(source, tokens, starts):
    ends = {}
    mixed = set()
    for t in tokens:
        if t.protected:
            continue
        for row in range(t.first - 1, t.last):
            begin = starts[row]
            if source[ends.get(row, begin) : max(begin, t.start)].strip():
                mixed.add(row)
            ends[row] = t.end
    return sum(
        row not in mixed
        and not source[
            pos : starts[row + 1] if row + 1 < len(starts) else len(source)
        ].strip()
        for row, pos in ends.items()
    )


def git(*args):
    return subprocess.check_output(["git", *args])


def tree(ref):
    return git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{tree}}").strip()


@contextmanager
def blob_reader():
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as p:

        def read(oid):
            p.stdin.write(oid + b"\n")
            p.stdin.flush()
            header = p.stdout.readline().split()
            if len(header) != 3 or header[1] != b"blob":
                raise ValueError(f"not a Git blob: {oid!r}")
            data = p.stdout.read(int(header[2]))
            if p.stdout.read(1) != b"\n":
                raise ValueError(f"truncated Git blob: {oid!r}")
            return data

        try:
            yield read
        finally:
            p.stdin.close()


def records(raw):
    return iter(raw.rstrip(b"\0").split(b"\0")) if raw else iter(())


def work_file(path):
    try:
        regular = stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return None
    return Path(path).read_bytes() if regular else None


def inventory_files(ref, paths, blob):
    if ref:
        for rec in records(git("ls-tree", "-rz", tree(ref), "--", *paths)):
            meta, raw_path = rec.split(b"\t", 1)
            mode, _, oid = meta.split()
            path = os.fsdecode(raw_path)
            if mode in REGULAR and (lang := language(path)):
                yield path, lang, blob(oid)
    else:
        raw = git(
            "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *paths
        )
        for raw_path in records(raw):
            path = os.fsdecode(raw_path)
            if (lang := language(path)) and (source := work_file(path)) is not None:
                yield path, lang, source


def inventory(args, blob):
    totals = Counter(
        files=0, tokens=0, protected=0, comment_only_lines=0, frozen_lines=0
    )
    for path, lang, source in inventory_files(args.revision, args.paths, blob):
        tokens, starts = scan(path, lang, source)
        only = comment_only_lines(source, tokens, starts)
        frozen = bool(FROZEN.match(path))
        totals.update(
            files=1,
            tokens=len(tokens),
            protected=sum(bool(t.protected) for t in tokens),
            comment_only_lines=only,
            frozen_lines=only if frozen else 0,
        )
        if args.jsonl:
            print(
                json.dumps(
                    {
                        "path": path,
                        "lang": lang,
                        "frozen": frozen,
                        "comment_only_lines": only,
                        "comments": [
                            {
                                "span": [t.start, t.end],
                                "lines": [t.first, t.last],
                                "protected": t.protected,
                                "text": source[t.start : t.end].decode(
                                    "utf-8", "replace"
                                ),
                            }
                            for t in tokens
                        ],
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    print(
        json.dumps(totals, sort_keys=True),
        file=sys.stderr if args.jsonl else sys.stdout,
    )


def changes(base, staged, paths, blob):
    args = [
        "diff",
        "--raw",
        "-z",
        "--no-abbrev",
        "--no-ext-diff",
        "--find-renames",
        "--diff-filter=ACMRT",
    ]
    if staged:
        args.append("--cached")
    it = records(git(*args, base, "--", *paths))
    for header in it:
        oldmode, newmode, old_oid, new_oid, status = header[1:].split()
        old = os.fsdecode(next(it))
        new = os.fsdecode(next(it)) if status.startswith((b"R", b"C")) else old
        if newmode not in REGULAR or not (new_lang := language(new)):
            continue
        source = blob(new_oid) if staged else work_file(new)
        if source is None:
            continue
        old_lang = (
            language(old)
            if oldmode in REGULAR and not status.startswith(b"C")
            else None
        )
        previous = blob(old_oid) if old_lang else b""
        yield old, old_lang, previous, new, new_lang, source
    if not staged:
        for raw in records(
            git("ls-files", "--others", "--exclude-standard", "-z", "--", *paths)
        ):
            path = os.fsdecode(raw)
            if (lang := language(path)) and (source := work_file(path)) is not None:
                yield path, None, b"", path, lang, source


def baseline(args):
    if args.base:
        return tree(args.base)
    event_base = None if args.staged else os.environ.get("COMMENT_LINT_BASE")
    if event_base:
        if set(event_base) == {"0"}:
            event_base = "HEAD^"
        ref = tree(event_base)
        if subprocess.run(
            ["git", "cat-file", "-e", ref + b":scripts/check_comments.py"],
            stderr=subprocess.DEVNULL,
        ).returncode:
            return None
        return ref
    if subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"], stdout=subprocess.DEVNULL
    ).returncode:
        return subprocess.check_output(
            ["git", "hash-object", "-t", "tree", "--stdin"], input=b""
        ).strip()
    return tree("HEAD")


def fingerprint(raw):
    return hashlib.blake2s(raw.replace(b"\r\n", b"\n"), digest_size=16).hexdigest()


def candidates(args, blob):
    base = baseline(args)
    if base is None:
        with ADOPTION_AUDIT.open(encoding="utf-8") as ledger:
            if (
                next(ledger).rstrip("\n")
                != "path\tclassifications\tbootstrap_fingerprints"
            ):
                raise ValueError(f"invalid comment census: {ADOPTION_AUDIT}")
            saved = {}
            for row in ledger:
                path, _, hashes = row.rstrip("\n").split("\t")
                if hashes != "-":
                    saved[path] = Counter(hashes.split())
        for path, lang, source in inventory_files(None, args.paths, blob):
            yield path, lang, source, saved.get(path, Counter())
        return
    for old, old_lang, previous, path, lang, source in changes(
        base, args.staged, args.paths, blob
    ):
        before = scan(old, old_lang, previous)[0] if old_lang else ()
        allowed = Counter(
            fingerprint(previous[t.start : t.end])
            for t in before
            if t.protected == "eslint-no-empty"
            or (not t.protected and FROZEN.match(old))
        )
        yield path, lang, source, allowed


def main():
    parser = argparse.ArgumentParser(
        description="Reject new explanatory comments; compare exact occurrences to Git."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--base",
        metavar="REF",
        help="compare working tree and untracked source against REF",
    )
    mode.add_argument(
        "--staged", action="store_true", help="compare the index against HEAD"
    )
    mode.add_argument(
        "--inventory",
        action="store_true",
        help="count tokens and physical comment-only lines",
    )
    mode.add_argument(
        "--test",
        action="store_true",
        help="run the parser and ratchet regression cases",
    )
    parser.add_argument(
        "--revision",
        metavar="REF",
        help="inventory a committed tree instead of local files",
    )
    parser.add_argument(
        "--jsonl", action="store_true", help="emit one inventory record per file"
    )
    parser.add_argument(
        "paths", nargs="*", help="repository-relative Git pathspecs (default: all)"
    )
    args = parser.parse_args()
    if (args.revision or args.jsonl) and not args.inventory:
        parser.error("--revision and --jsonl require --inventory")
    os.chdir(os.fsdecode(git("rev-parse", "--show-toplevel").rstrip(b"\n")))
    if args.test:
        import unittest

        suite = unittest.defaultTestLoader.discover(
            str(Path(__file__).parent), pattern="test_*comment*.py"
        )
        return int(not unittest.TextTestRunner().run(suite).wasSuccessful())
    with blob_reader() as blob:
        if args.inventory:
            inventory(args, blob)
            return 0
        count = 0
        for path, lang, source, allowed in candidates(args, blob):
            for t in scan(path, lang, source)[0]:
                catch_input = t.protected == "eslint-no-empty"
                if t.protected and not catch_input:
                    continue
                text = fingerprint(source[t.start : t.end])
                if (catch_input or FROZEN.match(path)) and allowed[text]:
                    allowed[text] -= 1
                else:
                    print(
                        f"{path}:{t.first}: new explanatory comment (review with /delete-comment)",
                        file=sys.stderr,
                    )
                    count += 1
        print(f"Comment ratchet: {count} new explanatory comment(s).")
        return int(count != 0)


if __name__ == "__main__":
    sys.exit(main())
