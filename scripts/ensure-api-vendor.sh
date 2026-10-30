#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
path=apps/api/app/vendor/TrackEval
if [ ! -f "$root/$path/trackeval_metrics/metrics.py" ]; then
  if ! git -C "$root" submodule update --init --recursive -- "$path"; then
    echo "Failed to initialize TrackEval submodule; check the checkout and network access." >&2
    exit 1
  fi
fi
if [ ! -f "$root/$path/trackeval_metrics/metrics.py" ]; then
  echo "TrackEval submodule is missing; run git submodule update --init --recursive -- $path" >&2
  exit 1
fi
