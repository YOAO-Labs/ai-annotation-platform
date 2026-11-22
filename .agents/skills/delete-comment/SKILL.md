---
name: delete-comment
description: Audit source comments, migrate a reviewed slice to ADRs or tracked issues, and tighten the repository comment ratchet. Use for /delete-comment with an optional repository-relative path.
---

# Delete a comment

Use the requested path, or the entire first-party repository if none is given. Record the starting Git revision. Run `pnpm --silent lint:comments --inventory --jsonl [path]` to locate tokens; for an exact historical inventory add `--revision <sha>`. Do not treat docstrings, strings containing comment delimiters, or Markdown as comments.

1. Read each comment with its code and consumers. Keywords are not proof: an alleged workaround that no longer exists is stale. Never remove a license, interpreter/tool directive, documentation-generator input, or byte-frozen migration artifact just to improve the count.
2. Classify each explanatory comment. Delete **redundant** narration and **stale** descriptions. For an **excuse**, confirm the shortcut still exists, search this repository's open and closed issues, and link or file a concrete issue with the owner, failure mode and acceptance criteria **before** deletion. Do not claim the implementation was fixed.
3. Move a **decision** to its owning record in `docs/adr/`, including the constraint, rejected alternative and consequence. Prefer the existing ADR, including accepted records in `archive/`; follow `docs/adr/README.md` if a new one is necessary. Do not leave a source breadcrumb. Preserve the original comment if the decision or tracking destination cannot yet be established.
4. Remove only the reviewed text. Compare program semantics, including JSX text and shell heredocs, and check real consumers (for example an executable docs snippet). Live first-party source has a zero prose budget. Frozen migrations and existing ESLint-consumed empty catches ratchet by exact occurrence, not an editable allowance. Check `pnpm lint:comments`; use `--staged` for the index or `--base <ref>` for the branch. Never expand the adoption census or transplant allowances.
5. Record original paths, ranges, dispositions, ADR evidence IDs and actual issue URLs. `docs/research/52-source-comment-audit.md` links the original and completion ledgers. Report physical source reduction separately from executable statements or net repository size: mixed lines do not disappear, while removing JSX wrappers or newly adjacent blank separators can reduce more lines than the lexical estimate.
