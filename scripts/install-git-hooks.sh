#!/usr/bin/env bash

set -u

if [ -n "${CI:-}" ]; then
  exit 0
fi

if [ ! -d ".git" ] && ! git rev-parse --git-dir >/dev/null 2>&1; then
  exit 0
fi

hooks_dir="$(git rev-parse --git-path hooks 2>/dev/null || echo ".git/hooks")"
if [ -f "$hooks_dir/pre-commit" ] && grep -q "pre-commit" "$hooks_dir/pre-commit" 2>/dev/null \
  && [ -f "$hooks_dir/pre-push" ] && grep -q "pre-commit" "$hooks_dir/pre-push" 2>/dev/null; then
  exit 0
fi

if command -v pre-commit >/dev/null 2>&1; then
  pre-commit install >/dev/null 2>&1 && \
    pre-commit install --hook-type pre-push >/dev/null 2>&1 && \
    echo "✓ pre-commit and pre-push hooks installed (commit: ruff / prettier / staged eslint / comments / naming; push: tsc / snapshots)"
else
  cat <<'EOF' >&2
⚠ pre-commit 未安装，建议执行（用 uv tool 独立安装，勿装进项目 venv）：
    uv tool install pre-commit && pre-commit install && pre-commit install --hook-type pre-push
  否则修改 apps/api/app/api 或 schemas 时需要手动跑：
    pnpm openapi:export
EOF
fi

exit 0
