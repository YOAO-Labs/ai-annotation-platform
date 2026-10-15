"""Prepare or apply a source-fenced, append-only nuScenes calibration repair.

Run from apps/api with its virtualenv. Prepare writes a local snapshot; apply
recomputes it under locks and rejects manual edits and changed inputs. No GET
request or ordinary task-open performs this mutation.
"""

import argparse
import asyncio
import json
from pathlib import Path
import uuid

from app.db.base import async_session
from app.db.enums import UserRole
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.user import User
from app.services.point_cloud_auxiliary import AuxiliaryError
from app.services.point_cloud_auxiliary.calibration import (
    apply_calibration_repair,
    plan_calibration_repair,
)


async def run(args):
    async with async_session() as db:
        task = await db.get(Task, args.task_id)
        actor = await db.get(User, args.actor_id)
        project = await db.get(Project, task.project_id) if task else None
        if (
            task is None
            or project is None
            or actor is None
            or not actor.is_active
            or (actor.role != UserRole.SUPER_ADMIN and project.owner_id != actor.id)
        ):
            raise AuxiliaryError("manager_required")
        if args.apply:
            prepared = json.loads(args.snapshot.read_text())
            result = await apply_calibration_repair(
                db, task, prepared, actor_id=actor.id
            )
            await db.commit()
        else:
            result = await plan_calibration_repair(db, task)
            # Do not overwrite an operator's existing review snapshot.
            with args.snapshot.open("x", encoding="utf-8") as output:
                json.dump(result, output, indent=2, allow_nan=False)
        print(json.dumps(result, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=uuid.UUID, required=True)
    parser.add_argument("--actor-id", type=uuid.UUID, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
