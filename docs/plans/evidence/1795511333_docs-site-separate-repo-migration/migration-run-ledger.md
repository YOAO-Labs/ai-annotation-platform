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
