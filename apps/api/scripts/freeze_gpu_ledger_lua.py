"""Compute SHA-256 of the final GPU-arbiter Redis Lua scripts.

Each script body is the fully composed string (shared preamble + body) that
Redis actually sees.

Usage
-----
    uv run python scripts/freeze_gpu_ledger_lua.py            # print table
    uv run python scripts/freeze_gpu_ledger_lua.py --json      # machine-readable
    uv run python scripts/freeze_gpu_ledger_lua.py --check <file>   # compare to golden

The final script names mirror the constants defined in
``aap_inference.gpu.ledger.scripts``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

# Attribute name on GPUArbiterStore -> final composed Lua string to freeze.
# Order matches GPUArbiterStore.__init__ registration order.
SCRIPT_SPECS: list[tuple[str, str]] = [
    ("mark_card_not_ready_script", "_MARK_CARD_NOT_READY_LUA"),
    ("begin_proof_reset_script", "_BEGIN_PROOF_RESET_LUA"),
    ("commit_proof_reset_script", "_COMMIT_PROOF_RESET_LUA"),
    ("reconcile_card_script", "_RECONCILE_CARD_LUA"),
    ("evolve_backend_domains_script", "_EVOLVE_BACKEND_DOMAINS_LUA"),
    ("collect_retired_backend_script", "_COLLECT_RETIRED_BACKEND_LUA"),
    ("verify_tombstone_gc_script", "_VERIFY_TOMBSTONE_GC_LUA"),
    ("admit_script", "_ADMIT_LUA"),
    ("lease_script", "_LEASE_LUA"),
    ("sweep_leases_script", "_SWEEP_LEASES_LUA"),
    ("queue_script", "_QUEUE_LUA"),
    ("transition_owner_script", "_TRANSITION_OWNER_LUA"),
    ("begin_idle_eviction_script", "_BEGIN_IDLE_EVICTION_LUA"),
    ("arm_eviction_cancel_script", "_ARM_EVICTION_CANCEL_LUA"),
    ("transition_script", "_TRANSITION_LUA"),
    ("installation_drain_script", "_INSTALLATION_DRAIN_LUA"),
]


def compute_digest(module) -> list[dict[str, str]]:
    """Return one {name, source_const, sha256, length} per final script."""
    rows: list[dict[str, str]] = []
    for _attr, const in SCRIPT_SPECS:
        body = getattr(module, const)
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        rows.append(
            {
                "name": const,
                "sha256": digest,
                "length": str(len(body)),
            }
        )
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    parser.add_argument(
        "--check",
        metavar="FILE",
        help="compare computed digests to a golden JSON file; exit 1 on mismatch",
    )
    args = parser.parse_args(argv)

    from aap_inference.gpu.ledger import scripts as lua_sources

    rows = compute_digest(lua_sources)

    if args.check:
        golden = json.loads(Path(args.check).read_text(encoding="utf-8"))
        if golden == rows:
            print(f"OK: {len(rows)} Lua digests match {args.check}")
            return 0
        print(f"MISMATCH against {args.check}", file=sys.stderr)
        for got, want in zip(rows, golden):
            if got != want:
                print(f"  - {got['name']}: got {got['sha256']}, want {want['sha256']}")
        return 1

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"{'#':>2}  {'name':<32}  {'length':>7}  sha256")
    for i, row in enumerate(rows, 1):
        print(f"{i:>2}  {row['name']:<32}  {row['length']:>7}  {row['sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
