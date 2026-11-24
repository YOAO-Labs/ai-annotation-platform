# Docs site migration run ledger

## PR-1 (2026-11-24, branch `docs/doc-site-remove`)

- **Fixture & asset decoupling**:
  - Copied `docs-site/public/home/sam-tools/smart-point.webm` (462097 bytes) to `apps/api/fixture-media/smart-point.webm` (`cmp -s` matched).
  - Removed `COPY docs-site/public/home/sam-tools/smart-point.webm` from `infra/docker/Dockerfile.api` and removed the 9-line `docs-site/` whitelist from `.dockerignore`.
  - Updated `_test_seed.py`, `_test_seed_filters.py`, `test_media_seed_workspace_native.py`, `test_media_seed_workspace_live.py`, and `test_worktree_filtering_identity_native.py` to reference `apps/api/fixture-media/smart-point.webm`.
  - Copied 4 README hero `.webp` images to `docs/assets/readme/` and updated `README.md`.
- **Examples & governance scripts**:
  - Moved `docs-site/dev/examples/{echo-ml-backend,mock-v2-backend}` to `examples/ml-backend/`.
  - Updated `.github/workflows/ci.yml`, `scripts/test-suites.json`, `scripts/test-suite-inventory.mjs`, `scripts/plan-e2e-suites.mjs`, `scripts/plan-e2e-suites.test.mjs`, `docs-site/dev/tutorials/ml-backend-starter.md`, `docs-site/dev/reference/ml-backend-protocol.md`, and `docs-site/api/guides/ml-backend.md`.
  - Moved `check-adr.mjs`, `check-plans-freshness.mjs`, and `check-changelog-version.mjs` to `scripts/` and updated `docs-site/package.json`, `.github/workflows/docs-validate.yml`, `.github/workflows/docs-acceptance.yml`, and `docs/plans/README.md`.
- **Unit evidence**:
  - `examples/ml-backend/echo-ml-backend`: `uv run --extra test pytest -q` -> `5 passed` (0.65s).
  - `examples/ml-backend/mock-v2-backend`: `uv run --extra test pytest -q` -> `15 passed` (0.68s).
  - `node --test scripts/*.test.mjs`: `91 passed, 1 failed`. The 1 failure is unrelated (`scripts/dev-worktree.test.mjs:130`, `ModuleNotFoundError: No module named 'httpx'` in worktree without `apps/api/.venv`). All touched test suites (`plan-e2e-suites.test.mjs`, `test-suite-inventory.test.mjs`, `check-workflow-names.test.mjs`, `image-reference-utils.test.mjs`) passed.
  - `node scripts/check-adr.mjs`: executed from `scripts/` and scanned `docs/adr/`; failed on pre-existing trunk duplicate ADR numbers (`0088`-`0107`) and 2 missing Status fields (`0174`, `0175`), unrelated to PR-1.
  - `node scripts/check-plans-freshness.mjs`: `0 stale plan(s) without Outcome`.
  - `node scripts/check-changelog-version.mjs`: `CHANGELOG v0.26.2 >= tag v0.26.2`.
  - `node scripts/check-workflow-names.mjs --strict`: `全部合规`.
  - `uvx --from ruff==0.15.22 ruff check` on 5 modified API Python files: `All checks passed!`.
  - `rg -n "docs-site/(public|dev/examples)" apps/ infra/ README.md .github/workflows/ci.yml`: 0 matches.
- **Live evidence**:
  - `docker build -f infra/docker/Dockerfile.api -t yoao-api-pr1-verify:local .` succeeded in 7.1s; `docker run --rm --entrypoint sh yoao-api-pr1-verify:local -c 'stat -c "%s %n" /app/fixture-media/smart-point.webm'` printed `462097 /app/fixture-media/smart-point.webm`; temporary image deleted immediately.
  - `pnpm --filter docs-site check:snippets`: `[check-doc-snippets] OK · 1 snippet 全部一致`.
  - `node scripts/check-dead-links.mjs docs-site`: `✓ 未发现死链（扫描 255 个 markdown 文件）`.
- **Perf evidence**:
  - `pnpm docs:build` baseline (pre-PR-1): `58.77s` total (`52.24s` VitePress).
  - `pnpm docs:build` post-PR-1: `61.80s` total (`50.78s` VitePress), +5.1% total / -2.8% VitePress (threshold: <= +15%).
  - Docker build context post-PR-1: `93.29MB` (`apps/api/fixture-media/smart-point.webm` replaces the same 462097-byte file previously whitelisted from `docs-site/`).

## PR-2 (2026-11-24, repo `YOAO-Labs/YOAO-Docs`, commit `d9891ad`)

- **Standalone repo bootstrap**:
  - Created public repository `YOAO-Labs/YOAO-Docs` and imported `docs-site/` (post-PR-1 state) promoted to repo root as a single initial commit (`598 files`, push pack `71.38 MiB`, well under the 100 MB threshold).
  - Enabled GitHub Pages with `build_type: workflow` (`https://yoao-labs.github.io/YOAO-Docs/`).
  - Dropped ADR (`mirror-adr.mjs`, `dev/adr/` sidebar) and ROADMAP (`ROADMAP` mirror group, `/roadmap/` nav) per operator directive; rewrote 224 internal ADR/ROADMAP and main-repo relative links across 67 files to `https://github.com/YOAO-Labs/YOAO/blob/main/...`.
  - Parameterized all cross-repo scripts via `scripts/_repo-root.mjs` (`YOAO_REPO_PATH`, defaulting to `../YOAO`), added `typescript` devDependency for standalone `generate-hotkeys.mjs` / `generate-settings.mjs`.
- **Unit evidence**:
  - `YOAO_REPO_PATH=<main-repo> pnpm check:all`: 100% pass (1 snippet, 248 frontmatter, 248 freshness, 79 images / 0 orphans, 5 reference tests, 3 diagram tests + 32 SVGs, 250 nav-covered pages, 249 readability files, 4 settings tests + 126 hotkeys + 48 fields + 755 API routes, 0 dead links across 256 markdown files).
  - Negative test (`YOAO_REPO_PATH=/nonexistent/yoao node scripts/sync-openapi.mjs`): exited 1 with explicit `YOAO_REPO_PATH` instruction.
- **Live evidence**:
  - `DOCS_BASE=/YOAO-Docs/ pnpm preview --port 4179` verified via `agent-browser` in isolated session: `openapi.json` & `llms.txt` HTTP 200; home title & 6-item nav (no Roadmap); `user-guide/` editLink -> `YOAO-Labs/YOAO-Docs`, `changelog/` editLink -> `YOAO-Labs/YOAO`; 0 console errors.
- **Perf evidence**:
  - `DOCS_BASE=/YOAO-Docs/ pnpm build`: `48.72s` total (`39.87s` VitePress), faster than main-repo baseline (`58.77s` total / `52.24s` VitePress).

## PR-3 (2026-11-24, repo `YOAO-Labs/YOAO-Docs`, commits `aa65280`, `f84142d`, `1029b8c`)

- **CI & Pages workflows**:
  - Added `.github/workflows/docs-validate.yml`, `.github/workflows/docs-sync.yml`, and `.github/workflows/docs-deploy.yml`.
  - Fixed two real CI issues caught on first runs: (1) materialized `public/openapi.json` before `pnpm check:all` dead-link check in `docs-validate.yml`; (2) added `apps/_shared/task_contract/src` to `sys.path`, pointed output to `DOCS_ROOT`, and added `actions: write` to `docs-sync.yml`.
- **Unit & CI evidence**:
  - `docs-validate.yml` run `45367365451` (`yoao_ref=docs/doc-site-remove`): **success** in 1m15s.
  - `docs-deploy.yml` run `45367018088` (`yoao_ref=docs/doc-site-remove`): **success** in 1m53s (build 1m02s, deploy 13s; threshold <= 5m).
  - `docs-sync.yml` run `45368038142` (`ref=docs/doc-site-remove`): **success** (regenerated tracked docs, pushed sync commit to `main`, and triggered `docs-deploy.yml`; org policy blocks GitHub Actions PR creation so sync commits directly to `main`).
- **Live evidence**:
  - Public site `https://yoao-labs.github.io/YOAO-Docs/` verified via `curl` and `agent-browser`: HTTP 200 on `/`, `/user-guide/`, `/dev/`, `/ops/`, `/api/`, `/changelog/`, `/openapi.json`, `/llms.txt`; home title & 6-item nav rendered with 0 console errors; `/changelog/` editLink points to `https://github.com/YOAO-Labs/YOAO/edit/main/CHANGELOG.md`.
- **Review gate**:
  - Paused here per plan for operator verification of live `https://yoao-labs.github.io/YOAO-Docs/` and `DOCS_SYNC_TOKEN` readiness before `PR-4`.

## PR-4 (2026-11-24, branch `docs/doc-site-remove`, commit `a85ad40f9`)

- **Cross-repo sync dispatch & static redirect**:
  - Added `.github/workflows/docs-sync-dispatch.yml` (`push` to `main`/`dev` on source-of-truth paths, `release: published`, `workflow_dispatch`) emitting `repository_dispatch` (`yoao-docs-sync`) to `YOAO-Labs/YOAO-Docs` using `DOCS_SYNC_TOKEN`.
  - Added `scripts/build-docs-redirect.mjs` and `scripts/build-docs-redirect.test.mjs`, and replaced `.github/workflows/docs-deploy.yml` to deploy the static redirect bundle.
- **Unit, Live & Perf evidence**:
  - `node scripts/check-workflow-names.mjs --strict`: `全部合规`.
  - `node --test scripts/build-docs-redirect.test.mjs`: `2 passed`.
  - Redirect bundle size: `6192 bytes` (~6 KB, threshold < 20 KB).
  - Live `agent-browser` against local HTTP server serving `404.html`: `/ai-annotation-platform/dev/reference/ml-backend-protocol?tab=v2#schema` redirected to `https://yoao-labs.github.io/YOAO-Docs/dev/reference/ml-backend-protocol?tab=v2#schema` (`ML Backend 协议契约 | YOAO`); `/YOAO/user-guide/` redirected to `https://yoao-labs.github.io/YOAO-Docs/user-guide/` (`用户手册 | YOAO`).

## PR-5 (2026-11-24, branch `docs/doc-site-remove`, commit `cdf0a372d`)

- **Removal & toolchain cleanup**:
  - Deleted `docs-site/`, `.github/workflows/docs-acceptance.yml`, `scripts/image-reference-utils.mjs`, `scripts/image-reference-utils.test.mjs` (614 files changed, 67,134 deletions).
  - Cleaned `pnpm-workspace.yaml`, `package.json`, `pnpm-lock.yaml` (`pnpm install --lockfile-only`, 11.9s), `.github/workflows/ci.yml`, `.github/workflows/docs-validate.yml`, `.github/workflows/claude-docs-impact.yml`, `.github/docs-impact-map.json`, `.pre-commit-config.yaml`, `.gitignore`, `.prettierignore`, `.dockerignore`, `.codex/environments/environment.toml`, `scripts/orca-worktree-setup.sh`, `scripts/test-orca-worktree-setup.py`, `scripts/check-dead-links.mjs`, `scripts/check-doc-version-prefix.mjs`, `scripts/check-product-naming.mjs`, `scripts/export_openapi.py`, and `packages/python-sdk/tests/test_docs_snippets.py`.
- **Unit & Perf evidence**:
  - `uv run --no-project --python 3.12 python scripts/test-orca-worktree-setup.py`: all 3 scenarios passed.
  - `uv run --no-project --with pytest pytest --noconftest -q packages/python-sdk/tests/test_docs_snippets.py`: `6 passed` in 0.02s.
  - `node --test scripts/plan-e2e-suites.test.mjs scripts/test-suite-inventory.test.mjs scripts/build-docs-redirect.test.mjs scripts/check-workflow-names.test.mjs`: `34 passed`.
  - Working tree size (excluding `.git` and `node_modules`): dropped from `514 MB` to `219 MB` (-295 MB total / ~85 MB tracked source, exceeding the 70 MB threshold).

## PR-6 (2026-11-24, branch `docs/doc-site-remove`)

- **Instructions, skills, README, DEV.md, articles & CHANGELOG**:
  - Updated `CLAUDE.md`, `README.md`, `DEV.md`, `CHANGELOG.md`, `.github/PULL_REQUEST_TEMPLATE.md`, `docs/plans/README.md`, `docs/adr/README.md`, `docs/articles/01-*.md`..`06-*.md`, and `.agents/skills/**` to point to `https://yoao-labs.github.io/YOAO-Docs/` and `YOAO-Labs/YOAO-Docs`.
- **Unit, Live & Perf evidence**:
  - `rg -n "docs-site/|yoao-labs\.github\.io/ai-annotation-platform" CLAUDE.md README.md DEV.md .github/PULL_REQUEST_TEMPLATE.md docs/plans/README.md docs/adr/README.md docs/articles/ .agents/skills/`: 0 matches.
  - `node scripts/check-dead-links.mjs docs/articles`: `0` dead links in `0.11s` (threshold < 5s).
  - `node scripts/check-doc-version-prefix.mjs --files=README.md,DEV.md`: clean.
  - All `https://yoao-labs.github.io/YOAO-Docs/...` links in `README.md`, `DEV.md`, `CLAUDE.md` verified HTTP 200 via `curl`.
