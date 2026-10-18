#!/usr/bin/env python3
"""Regenerate or check the standalone HTTP contract snapshot."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aap_media_probe.app import create_app  # noqa: E402

OUTPUT = ROOT / "openapi.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check", action="store_true", help="fail if openapi.json is stale"
    )
    args = parser.parse_args()
    schema = create_app(token="openapi-generation-token-000000000000").openapi()
    if args.check:
        if not OUTPUT.exists() or json.loads(OUTPUT.read_text()) != schema:
            print(f"{OUTPUT} is stale; regenerate it without --check", file=sys.stderr)
            return 1
        print("media-probe OpenAPI snapshot is current")
        return 0
    OUTPUT.write_text(
        json.dumps(
            schema, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )
    print(f"wrote {OUTPUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
