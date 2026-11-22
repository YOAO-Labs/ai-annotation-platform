#!/usr/bin/env bash
set -euo pipefail

root=$(cd "${ORCA_ROOT_PATH:?Run this script through Orca}" && pwd -P)
cd "${ORCA_WORKTREE_PATH:?Run this script through Orca}"
if [ "$root" = "$(pwd -P)" ]; then
  echo "Setup requires a separate worktree." >&2
  exit 1
fi

link_from_root() {
  if [ ! -e "$1" ] && [ ! -L "$1" ]; then
    ln -s "$root/$1" "$1"
    echo "Linked $1"
  fi
}

if [ ! -e "$root/.env" ] && [ ! -L "$root/.env" ]; then
  (umask 077; printf '# Shared local configuration for Orca worktrees.\n' > "$root/.env")
fi
link_from_root .env
if [ -f "$root/.env.local" ]; then
  link_from_root .env.local
fi

share_dependencies=true
for path in pnpm-lock.yaml pnpm-workspace.yaml package.json apps/web/package.json docs-site/package.json; do
  if ! cmp -s "$root/$path" "$path"; then
    share_dependencies=false
  fi
done
if ! cmp -s "$root/pnpm-lock.yaml" "$root/node_modules/.pnpm/lock.yaml"; then
  share_dependencies=false
fi
for path in node_modules apps/web/node_modules docs-site/node_modules; do
  if [ ! -d "$root/$path" ]; then
    share_dependencies=false
  fi
done
if [ "$share_dependencies" = true ]; then
  for path in node_modules apps/web/node_modules docs-site/node_modules; do
    link_from_root "$path"
  done
else
  for path in node_modules apps/web/node_modules docs-site/node_modules; do
    if [ -L "$path" ]; then
      echo "Dependencies differ: detach $path before installing worktree dependencies." >&2
      exit 1
    fi
  done
  pnpm install --frozen-lockfile
fi

for path in apps/api/.venv apps/web/src/api/generated; do
  if [ -L "$path" ]; then
    echo "$path must be local to this worktree; detach its symlink before setup." >&2
    exit 1
  fi
done
bash scripts/ensure-api-vendor.sh
UV_PROJECT_ENVIRONMENT="$PWD/apps/api/.venv" uv sync --project apps/api --locked --extra test
pnpm codegen
