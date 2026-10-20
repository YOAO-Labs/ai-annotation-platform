"""Fresh Celery process with source and capability evidence for account fences."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys


def main():
    api = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(api))
    from celery.signals import worker_ready
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    from app.workers.celery_app import celery_app
    from app.workers import cross_frame_job

    proof = Path(os.environ["AAP_ACCOUNT_FENCE_WORKER_PROOF"])
    trace = Path(os.environ["AAP_ACCOUNT_FENCE_WORKER_TRACE"])
    files = (
        "app/workers/cross_frame_job.py",
        "app/services/identity_account_lock_data.py",
        "app/services/project_write_guard.py",
    )

    def capture(_conn, _cursor, statement, *_args):
        for capability in ("read_actor_facts", "lock_accounts_share"):
            if f"aap_identity_v1.{capability}(" in statement:
                with trace.open("a", encoding="utf-8") as output:
                    output.write(json.dumps({"capability": capability}) + "\n")

    event.listen(Engine, "before_cursor_execute", capture)

    def ready(**_kwargs):
        assert cross_frame_job.run_cross_frame_job.name in celery_app.tasks
        proof.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "source_hashes": {
                        name: hashlib.sha256((api / name).read_bytes()).hexdigest()
                        for name in files
                    },
                    "task": cross_frame_job.run_cross_frame_job.name,
                }
            )
        )

    worker_ready.connect(ready, weak=False)
    celery_app.worker_main(sys.argv[1:])


if __name__ == "__main__":
    main()
