"""Publish verified current-frame experiment assets as immutable platform PCD.

The explicit mapping is JSON: [{"task_id": "UUID", "frame": 0}, ...]. Tokens,
source hashes, primary PCD rows and every per-exposure calibration must agree.
Use --check first; it verifies without creating database rows or storage objects.
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
from app.services.point_cloud_auxiliary import (
    AuxiliaryError,
    fail_generation,
    publish_auxiliary,
    reserve_generation,
    resolve_task_inputs,
)
from app.services.point_cloud_auxiliary.prepared import prepare_experiment_frame


async def run(args):
    mapping = json.loads(args.mapping.read_text())
    if not isinstance(mapping, list) or not 1 <= len(mapping) <= 1000:
        raise AuxiliaryError("prepared_mapping_invalid")
    for entry in mapping:
        async with async_session() as db:
            task = await db.get(Task, uuid.UUID(entry["task_id"]))
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
            inputs = await resolve_task_inputs(db, task, "omega-current-mono-512")
            clouds, provenance = await asyncio.to_thread(
                prepare_experiment_frame,
                args.bundle,
                manifest_sha256=args.manifest_sha256,
                frame=int(entry["frame"]),
                inputs=inputs,
            )
            if args.check:
                print(
                    json.dumps(
                        {
                            "task_id": str(task.id),
                            "input_digest": inputs.input_digest,
                            "status": "verified",
                            "cameras": {
                                role: len(cloud["xyz"])
                                for role, cloud in clouds.items()
                            },
                        }
                    )
                )
                continue
            generation = await reserve_generation(db, task, inputs)
            await db.commit()
            lease = generation.lease_token
            try:
                result = await publish_auxiliary(
                    db, task, inputs, clouds, provenance, generation=generation
                )
                await db.commit()
            except Exception as exc:
                identity = (generation.asset_id, generation.generation)
                await db.rollback()
                if lease is not None:
                    await fail_generation(
                        db,
                        *identity,
                        lease_token=lease,
                        reason=exc.code
                        if isinstance(exc, AuxiliaryError)
                        else "publication_failed",
                    )
                    await db.commit()
                raise
            print(
                json.dumps(
                    {
                        "task_id": str(task.id),
                        "asset_id": str(result.asset_id),
                        "generation": result.generation,
                        "status": result.status,
                    }
                )
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--mapping", type=Path, required=True)
    parser.add_argument("--actor-id", type=uuid.UUID, required=True)
    parser.add_argument("--check", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
