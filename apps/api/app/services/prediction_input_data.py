"""Assets input lease commands on the caller's existing transaction."""

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

_CREATE = text(
    "SELECT expires_at, retain_until FROM yoao_assets_v1.create_prediction_input_lease("
    "CAST(:id AS uuid), CAST(:owner_id AS uuid), CAST(:owner_kind AS text), "
    "CAST(:task_id AS uuid), CAST(:contract_version AS integer), CAST(:storage_ref AS jsonb), "
    "CAST(:maintenance_revision AS integer), CAST(:source_ref AS jsonb), CAST(:started AS timestamptz))"
).bindparams(
    bindparam("storage_ref", type_=JSONB),
    bindparam("source_ref", type_=JSONB(none_as_null=True)),
)
_LEASE = text(
    "SELECT * FROM yoao_assets_v1.prediction_input_lease_facts("
    "CAST(:id AS uuid), CAST(:lock AS boolean), CAST(:due_at AS timestamptz))"
)
_PENDING = text(
    "SELECT yoao_assets_v1.prediction_input_objects_pending(CAST(:id AS uuid))"
)
_INVENTORY = text(
    "SELECT yoao_assets_v1.read_prediction_input_inventory(CAST(:id AS uuid))"
)
_OBJECT = text(
    "SELECT yoao_assets_v1.read_prediction_input_object(CAST(:id AS uuid), CAST(:relative AS text))"
)
_RESERVE = text(
    "SELECT yoao_assets_v1.reserve_prediction_input_object("
    "CAST(:id AS uuid), CAST(:relative AS text), CAST(:planned AS jsonb))"
).bindparams(bindparam("planned", type_=JSONB))
_VERIFY = text(
    "SELECT yoao_assets_v1.verify_prediction_input_object("
    "CAST(:id AS uuid), CAST(:relative AS text), CAST(:expected AS jsonb), "
    "CAST(:etag AS text), CAST(:version_id AS text))"
).bindparams(bindparam("expected", type_=JSONB))
_SEAL = text(
    "SELECT yoao_assets_v1.seal_prediction_input_lease("
    "CAST(:id AS uuid), CAST(:uncertain AS boolean), CAST(:closed_at AS timestamptz))"
)
_PREPARE = text(
    "SELECT yoao_assets_v1.prepare_prediction_input_cleanup("
    "CAST(:id AS uuid), CAST(:checked_at AS timestamptz))"
)
_FAILED = text(
    "SELECT yoao_assets_v1.record_prediction_input_cleanup_failure("
    "CAST(:id AS uuid), CAST(:code AS text), CAST(:uncertain AS boolean))"
)
_FINISH = text(
    "SELECT yoao_assets_v1.finish_prediction_input_cleanup("
    "CAST(:id AS uuid), CAST(:permit_id AS uuid), CAST(:permit_revision AS integer), "
    "CAST(:finished_at AS timestamptz))"
)
_DUE = text(
    "SELECT id FROM yoao_assets_v1.list_due_prediction_input_cleanups("
    "CAST(:checked_at AS timestamptz), CAST(:limit AS integer))"
)


async def create_lease(
    db,
    identifier,
    *,
    owner_id,
    owner_kind,
    task_id,
    contract_version,
    storage_ref,
    maintenance_revision,
    source_ref,
    started,
):
    return (
        await db.execute(
            _CREATE,
            {
                "id": identifier,
                "owner_id": owner_id,
                "owner_kind": owner_kind,
                "task_id": task_id,
                "contract_version": contract_version,
                "storage_ref": storage_ref,
                "maintenance_revision": maintenance_revision,
                "source_ref": source_ref,
                "started": started,
            },
        )
    ).one()


async def lease_facts(db, identifier, *, lock=False, due_at=None):
    if db.autoflush:
        await db.flush()
    return (
        await db.execute(_LEASE, {"id": identifier, "lock": lock, "due_at": due_at})
    ).one_or_none()


async def objects_pending(db, identifier):
    return await db.scalar(_PENDING, {"id": identifier})


async def read_inventory(db, identifier):
    return await db.scalar(_INVENTORY, {"id": identifier})


async def read_object(db, identifier, relative):
    return await db.scalar(_OBJECT, {"id": identifier, "relative": relative})


async def reserve_object(db, identifier, relative, planned):
    return await db.scalar(
        _RESERVE, {"id": identifier, "relative": relative, "planned": planned}
    )


async def verify_object(db, identifier, relative, expected, *, etag, version_id):
    return await db.scalar(
        _VERIFY,
        {
            "id": identifier,
            "relative": relative,
            "expected": expected,
            "etag": etag,
            "version_id": version_id,
        },
    )


async def seal_lease(db, identifier, *, uncertain, closed_at):
    await db.execute(
        _SEAL, {"id": identifier, "uncertain": uncertain, "closed_at": closed_at}
    )


async def prepare_cleanup(db, identifier, *, checked_at):
    return await db.scalar(_PREPARE, {"id": identifier, "checked_at": checked_at})


async def record_cleanup_failure(db, identifier, *, code, uncertain):
    await db.execute(_FAILED, {"id": identifier, "code": code, "uncertain": uncertain})


async def finish_cleanup(db, identifier, *, permit_id, permit_revision, finished_at):
    return await db.scalar(
        _FINISH,
        {
            "id": identifier,
            "permit_id": permit_id,
            "permit_revision": permit_revision,
            "finished_at": finished_at,
        },
    )


async def due_cleanups(db, *, checked_at, limit):
    return (await db.scalars(_DUE, {"checked_at": checked_at, "limit": limit})).all()
