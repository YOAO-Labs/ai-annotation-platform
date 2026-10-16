---
name: aap-doc-media
description: Capture qualified marketing masters and derive this project's documentation videos, GIFs, and posters from those masters; also maintain static screenshots, capture isolation, provenance, and media-review records.
---

# Project media from qualified masters

Start with `pnpm docs:media:audit` for provenance/review failures, or the requested asset/flow for capture work. Distinguish a missing file, changed content hash, outdated generation source, and stale/unreachable review commit. Regeneration is not the default fix for every audit failure.

## Choose the evidence

- `apps/web/e2e/screenshots/outputs/manifest.json`: static image generation.
- `apps/web/e2e/screenshots/outputs/flow-manifest.json`: recordings and derived media.
- `docs-site/maintainers/media-reviews.json`: visual/content review evidence.
- `apps/web/e2e/screenshots/scenes/` and `flows/`: reproducible product interactions.

Read [capture](references/capture.md) when generating or rerecording assets. Inspect the actual image and, for video, its beginning, core interaction, ending, and poster. Verify the represented feature, loading state, real target geometry, and sensitive-data masking. Successful encoding is not visual acceptance.

Use one production video workflow: capture a qualified `marketing` master, archive it, then derive every requested resolution, frame rate, GIF, and poster from that same source. This applies to ML and non-ML recordings. Standard `docs` recording is for functional rehearsal/diagnosis, not a second production source. Missing hardware or model services are blockers to report or resolve, not permission to downgrade. Existing standard archives retain their truthful provenance; do not relabel them or silently regenerate them. Static screenshots remain independent.

For video work across macOS and Linux, select inference requirements separately from capture quality. Use `pnpm --filter @anno/web screenshots:record -- --list`, then `--flow <id> --plan` before recording. All flow recording commands archive sources only. Publish video, GIF, and poster derivatives through `pnpm docs:media:derive` using its default `marketing` quality. Explicit `standard` support is for previously archived material when its reuse is requested, not the routine refresh workflow. Read the cross-platform and derivation sections in the capture reference before preparing either environment.

## Repair provenance honestly

Review metadata and generation provenance answer different questions. After merge or rebase, check commit reachability, file hashes, associated paths, and actual review scope. Preserve valid records from both sides. Do not fabricate a clean generation commit, change hashes to silence failures, or relabel an unreviewed set as freshly reviewed.

Use `pnpm docs:media:approve -- --asset <repository-relative-path>` only after a new completed visual/content review within the user's authorization. It requires a clean tree and committed assets, and records current HEAD, time, and reviewer. Prepare authorized changes first; if review itself is missing, report that exact gap. Agent sampling is not a fresh human review of every asset.

For history-only repair after merge/rebase, do not run the approval command to manufacture freshness. Preserve the original review time and reviewer; repair a commit reference only with demonstrated equivalence of the reviewed asset and watched source scope. Otherwise retain the evidence gap.

Run the relevant audit mode: normal/strict validation for the requested change, `--release` when release provenance is required. Derive current counts from manifests and files. Do not rewrite unrelated assets or use blanket baseline regeneration to make CI green.

The detailed maintainer workflow is [update-screenshots.md](../../../docs-site/dev/how-to/update-screenshots.md). Keep published assets and provenance together; retain private capture masters until their requested backup is verified.
