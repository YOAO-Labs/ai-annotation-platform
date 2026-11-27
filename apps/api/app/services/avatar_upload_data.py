"""Caller-transaction bindings for the fixed avatar receipt capabilities."""

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

_RESERVE = text(
    "SELECT yoao_identity_v1.reserve_avatar_upload("
    ":actor_id, :token, :storage_ref, :sha256, :size_bytes)"
).bindparams(bindparam("storage_ref", type_=JSONB))
_CONFIRM_VERSION = text(
    "SELECT yoao_identity_v1.confirm_avatar_upload_version("
    ":actor_id, :token, :storage_ref, :sha256, :size_bytes, :object_version_id)"
).bindparams(bindparam("storage_ref", type_=JSONB))
_PENDING_MATCHES = text(
    "SELECT yoao_identity_v1.avatar_upload_pending_matches("
    ":actor_id, :token, :storage_ref, :sha256, :size_bytes, :object_version_id)"
).bindparams(bindparam("storage_ref", type_=JSONB))
_READ_READY = text("SELECT * FROM yoao_identity_v1.read_ready_avatar_upload(:token)")
_LIST_RETIRED = text("SELECT yoao_identity_v1.list_avatar_retirements(:limit)")
_BEGIN_RETIREMENT = text(
    "SELECT * FROM yoao_identity_v1.begin_avatar_retirement(:token)"
)
_FINISH_RETIREMENT = text(
    "SELECT yoao_identity_v1.finish_avatar_retirement("
    ":token, :storage_ref, :sha256, :size_bytes, :object_version_id)"
).bindparams(bindparam("storage_ref", type_=JSONB))
_BINDING_HISTORY = text("SELECT * FROM yoao_identity_v1.read_avatar_binding_history()")


def _facts(receipt):
    return {
        "token": receipt.token,
        "storage_ref": receipt.storage_ref,
        "sha256": receipt.sha256,
        "size_bytes": receipt.size_bytes,
    }


def _versioned_facts(receipt):
    return dict(_facts(receipt), object_version_id=receipt.object_version_id)


async def reserve(db, actor_id, receipt):
    await db.flush()
    return await db.scalar(_RESERVE, dict(_facts(receipt), actor_id=actor_id))


async def confirm_version(db, actor_id, receipt, version):
    if db.autoflush:
        await db.flush()
    return await db.scalar(
        _CONFIRM_VERSION,
        dict(_facts(receipt), actor_id=actor_id, object_version_id=version),
    )


async def pending_matches(db, actor_id, receipt):
    if db.autoflush:
        await db.flush()
    return await db.scalar(
        _PENDING_MATCHES, dict(_versioned_facts(receipt), actor_id=actor_id)
    )


async def read_ready(db, token):
    if db.autoflush:
        await db.flush()
    return (await db.execute(_READ_READY, {"token": token})).one_or_none()


async def list_retired(db, limit):
    if db.autoflush:
        await db.flush()
    return (await db.scalars(_LIST_RETIRED, {"limit": limit})).all()


async def begin_retirement(db, token):
    if db.autoflush:
        await db.flush()
    return (await db.execute(_BEGIN_RETIREMENT, {"token": token})).one_or_none()


async def finish_retirement(db, receipt):
    if db.autoflush:
        await db.flush()
    deleted = await db.scalar(_FINISH_RETIREMENT, _versioned_facts(receipt))
    if deleted:
        for (cls, identity, _token), instance in list(
            db.sync_session.identity_map.items()
        ):
            if cls.__name__ == "AvatarUpload" and identity == (receipt.token,):
                db.expunge(instance)
    return deleted


async def binding_history(db):
    if db.autoflush:
        await db.flush()
    return dict((await db.execute(_BINDING_HISTORY)).mappings().one())
