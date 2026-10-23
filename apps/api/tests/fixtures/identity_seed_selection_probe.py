"""Probe the installed scale-seed production guard in a child process."""

from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> None:
    if sys.argv[1:] != ["production_guard"]:
        raise SystemExit("usage: identity_seed_selection_probe.py production_guard")

    environment = os.environ.copy()
    environment["ENVIRONMENT"] = "production"
    # If the early guard regresses, an accidentally executed seed command can
    # only fail to connect to this deliberately unreachable local endpoint.
    environment["DATABASE_URL"] = (
        "postgresql+asyncpg://seed_probe:unreachable@127.0.0.1:1/guard_probe"
    )
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.seed_scale", "4", "2", "1"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    print(
        "AAP_IDENTITY_SEED_SELECTION_RESULT="
        + json.dumps(
            {
                "operation": "production_guard",
                "exit_code": completed.returncode,
                "stdout": completed.stdout[-1000:],
                "stderr": completed.stderr[-1000:],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
