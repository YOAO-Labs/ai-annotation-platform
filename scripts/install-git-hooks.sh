#!/usr/bin/env bash

set -u

if [ -n "${CI:-}" ]; then
  exit 0
fi

if [ ! -d ".git" ] && ! git rev-parse --git-dir >/dev/null 2>&1; then
  exit 0
fi

if [ -f ".git/hooks/pre-commit" ] && grep -q "pre-commit" ".git/hooks/pre-commit" 2>/dev/null; then
  exit 0
fi

if command -v pre-commit >/dev/null 2>&1; then
  pre-commit install >/dev/null 2>&1 && \
    echo "✓ pre-commit hooks installed (ruff / eslint / tsc / openapi-snapshot)"
else
  cat <<'EOF' >&2
⚠ pre-commit 未安装，建议执行（用 uv tool 独立安装，勿装进项目 venv）：
    uv tool install pre-commit && pre-commit install
  否则修改 apps/api/app/api 或 schemas 时需要手动跑：
    pnpm openapi:export
EOF
fi

exit 0
