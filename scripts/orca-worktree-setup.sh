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
for path in pnpm-lock.yaml pnpm-workspace.yaml package.json apps/web/package.json; do
  if ! cmp -s "$root/$path" "$path"; then
    share_dependencies=false
  fi
done
if ! cmp -s "$root/pnpm-lock.yaml" "$root/node_modules/.pnpm/lock.yaml"; then
  share_dependencies=false
fi
for path in node_modules apps/web/node_modules; do
  if [ ! -d "$root/$path" ]; then
    share_dependencies=false
  fi
done
if [ "$share_dependencies" = true ]; then
  for path in node_modules apps/web/node_modules; do
    link_from_root "$path"
  done
else
  for path in node_modules apps/web/node_modules; do
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
uv run --no-project --python '>=3.11' -- python - "$root" <<'PY'
import hashlib
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import tomllib

worktree = Path.cwd()
api = Path("apps/api")
with (api / "uv.lock").open("rb") as lock:
    packages = tomllib.load(lock)["package"]
runtime = next(package for package in packages if package["name"] == "yoao-backend-runtime")
relative_wheel = Path(os.path.normpath(api / runtime["source"]["path"]))
wheel = worktree / relative_wheel
if wheel.resolve() != wheel:
    sys.exit(f"Plugin-SDK wheel must be local to this worktree: {wheel}")
source = wheel if wheel.exists() else Path(sys.argv[1]) / relative_wheel
instructions = (
    f"Stage the matching wheel at {wheel} with scripts/fetch-python-artifacts.py, "
    "then rerun setup. See docs-site/dev/tutorials/local-dev.md."
)
if not source.is_file():
    sys.exit(f"Missing locked Plugin-SDK wheel: {source}\n{instructions}")
data = source.read_bytes()
if f"sha256:{hashlib.sha256(data).hexdigest()}" != runtime["wheels"][0]["hash"]:
    sys.exit(f"Plugin-SDK wheel does not match apps/api/uv.lock: {source}\n{instructions}")
if source != wheel:
    wheel.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=wheel.parent) as temporary:
        staged = Path(temporary) / wheel.name
        staged.write_bytes(data)
        staged.replace(wheel)
    print(f"Staged {relative_wheel}")
PY
UV_PROJECT_ENVIRONMENT="$PWD/apps/api/.venv" uv sync --project apps/api --locked --extra test
pnpm codegen
