from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from uuid import UUID

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB


@dataclass(frozen=True, slots=True)
class AssetFacts:
    id: UUID
    dataset_item_id: UUID | None
    task_id: UUID | None
    profile_version: str
    active_generation: int | None
    building_generation: int | None


@dataclass(frozen=True, slots=True)
class GenerationFacts:
    asset_id: UUID
    generation: int
    job_id: UUID | None
    source_snapshot: str | None
    policy: dict | None
    original_ref: dict | None
    auth_ref: dict | None
    output_id: UUID | None
    source_identity: str
    source_fingerprint: str | None
    status: str
    lease_token: UUID | None
    lease_expires_at: datetime | None
    width: int | None
    height: int | None
    tile_size: int
    overlap: int
    max_level: int | None
    format: str | None
    normalization_version: str
    manifest_key: str | None
    manifest_digest: str | None
    overview_key: str | None
    overview_width: int | None
    overview_height: int | None
    overview_digest: str | None
    tile_count: int | None
    retained_bytes: int | None
    error_code: str | None
    attempts: int
    last_accessed_at: datetime | None
    updated_at: datetime


_ASSET_FIELDS = tuple(field.name for field in fields(AssetFacts))
_GENERATION_FIELDS = tuple(field.name for field in fields(GenerationFacts))
_ASSET_PROJECTION = """
    (asset).id AS id, (asset).dataset_item_id AS dataset_item_id, (asset).task_id AS task_id,
    (asset).profile_version AS profile_version, (asset).active_generation AS active_generation,
    (asset).building_generation AS building_generation
"""
_JSON = JSONB(none_as_null=True)

_ASSET = text(
    "SELECT * FROM yoao_assets_v1.read_legacy_pyramid_asset(:kind,:owner,:profile)"
)
_GENERATION = text(
    "SELECT * FROM yoao_assets_v1.read_legacy_pyramid_generation(:asset,:number)"
)
_SUMMARIES = text(f"""
    SELECT {_ASSET_PROJECTION}, (selected).*
    FROM yoao_assets_v1.read_legacy_pyramid_summaries(
        CAST(:items AS uuid[]),CAST(:tasks AS uuid[]),:profile
    )
""")
_PREPARE = text("""
    SELECT reused_inflight, (selected).*
    FROM yoao_assets_v1.prepare_legacy_pyramid_generation(
        :asset,:kind,:owner,:profile,:identity,:original,:authority,:normalization,:force,:keep_inflight
    )
""").bindparams(bindparam("original", type_=_JSON), bindparam("authority", type_=_JSON))
_LOCK_CLAIM = text("SELECT yoao_assets_v1.lock_legacy_pyramid_claim(:asset,:number)")
_CLAIM = text("""
    SELECT yoao_assets_v1.claim_legacy_pyramid_generation(:asset,:number,:output,:token,:seconds,:ref)
""").bindparams(bindparam("ref", type_=_JSON))
_LOCK_PUBLISH = text(f"""
    SELECT {_ASSET_PROJECTION}, (selected).*
    FROM yoao_assets_v1.lock_legacy_pyramid_publication(:asset,:number,:token,:output)
""")
_CONTEXT = text("""
    SELECT (asset).dataset_item_id AS dataset_item_id, (asset).task_id AS task_id,
        (asset).active_generation AS active_generation, (asset).building_generation AS building_generation,
        (selected).original_ref AS original_ref, (selected).status AS status,
        (selected).lease_token AS generation_token, (selected).lease_expires_at AS lease_expires_at,
        (output).*
    FROM yoao_assets_v1.read_legacy_pyramid_context(:asset,:number,:output,:lock)
""")
_OUTPUT = text("SELECT * FROM yoao_assets_v1.read_legacy_pyramid_output(:output,:lock)")
_LOCK_OUTPUT = text("""
    SELECT * FROM yoao_assets_v1.lock_legacy_pyramid_output_for_publish(:asset,:number,:token,:output)
""")
_PUBLISH = text("""
    SELECT yoao_assets_v1.publish_legacy_pyramid_generation(
        :asset,:number,:token,:output,:result,:inventory,:revision
    )
""").bindparams(bindparam("result", type_=_JSON), bindparam("inventory", type_=_JSON))
_FAIL = text(
    "SELECT yoao_assets_v1.fail_legacy_pyramid_generation(:asset,:number,:token,:error)"
)
_INVALIDATE = text(
    "SELECT yoao_assets_v1.invalidate_legacy_pyramid_generation(:asset,:number,:output,:error)"
)
_TOUCH = text(
    "SELECT yoao_assets_v1.touch_legacy_pyramid_generation(:asset,:number,:output)"
)
_EXPIRED = text(
    "SELECT * FROM yoao_assets_v1.scan_expired_legacy_pyramid_generations()"
)
_STALE = text(
    "SELECT * FROM yoao_assets_v1.scan_stale_legacy_pyramid_generations(:cutoff)"
)
_REMOVE_STALE = text(
    "SELECT yoao_assets_v1.remove_stale_legacy_pyramid_generation(:asset,:number,:cutoff)"
)
_RETIRABLE = text(
    "SELECT * FROM yoao_assets_v1.scan_retirable_legacy_pyramid_outputs(:cutoff)"
)
_BEGIN_RETIREMENT = text(
    "SELECT * FROM yoao_assets_v1.begin_legacy_pyramid_retirement(:output,:cutoff)"
)
_CHECK_RETIREMENT = text("""
    SELECT yoao_assets_v1.check_legacy_pyramid_retirement(:output,:ref,:permit)
""").bindparams(bindparam("ref", type_=_JSON))
_FINISH_RETIREMENT = text("""
    SELECT yoao_assets_v1.finish_legacy_pyramid_retirement(:output,:ref)
""").bindparams(bindparam("ref", type_=_JSON))
_RETAINED = text("SELECT yoao_assets_v1.read_legacy_pyramid_retained_bytes()")


def _asset(row) -> AssetFacts:
    return AssetFacts(*(row[name] for name in _ASSET_FIELDS))


def _generation(row) -> GenerationFacts:
    return GenerationFacts(*(row[name] for name in _GENERATION_FIELDS))


async def _execute(db, statement, args=None):
    if db.autoflush:
        await db.flush()
    return await db.execute(statement, args)


async def _scalar(db, statement, args=None):
    return (await _execute(db, statement, args)).scalar()


async def read_asset(db, kind, owner, profile) -> AssetFacts | None:
    args = {"kind": kind, "owner": owner, "profile": profile}
    row = (await _execute(db, _ASSET, args)).mappings().one_or_none()
    return _asset(row) if row is not None else None


async def read_generation(db, asset, number=None) -> GenerationFacts | None:
    args = {"asset": asset, "number": number}
    row = (await _execute(db, _GENERATION, args)).mappings().one_or_none()
    return _generation(row) if row is not None else None


async def summaries(db, items, tasks, profile):
    rows = await _execute(
        db, _SUMMARIES, {"items": items, "tasks": tasks, "profile": profile}
    )
    return [
        (_asset(row), _generation(row) if row["asset_id"] is not None else None)
        for row in rows.mappings()
    ]


async def prepare(
    db,
    *,
    asset,
    kind,
    owner,
    profile,
    identity,
    original,
    authority,
    normalization,
    force,
    keep_inflight,
):
    row = (
        (
            await _execute(
                db,
                _PREPARE,
                {
                    "asset": asset,
                    "kind": kind,
                    "owner": owner,
                    "profile": profile,
                    "identity": identity,
                    "original": original,
                    "authority": authority,
                    "normalization": normalization,
                    "force": force,
                    "keep_inflight": keep_inflight,
                },
            )
        )
        .mappings()
        .one()
    )
    return _generation(row), row["reused_inflight"]


async def lock_claim(db, asset, number) -> bool:
    return await _scalar(db, _LOCK_CLAIM, {"asset": asset, "number": number}) is True


async def claim(db, asset, number, output, token, seconds, reference) -> bool:
    return (
        await _scalar(
            db,
            _CLAIM,
            {
                "asset": asset,
                "number": number,
                "output": output,
                "token": token,
                "seconds": seconds,
                "ref": reference,
            },
        )
        is True
    )


async def lock_publication(db, asset, number, token, output):
    row = (
        (
            await _execute(
                db,
                _LOCK_PUBLISH,
                {
                    "asset": asset,
                    "number": number,
                    "token": token,
                    "output": output,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    return (_asset(row), _generation(row)) if row is not None else None


async def read_context(db, asset, number, output, *, lock):
    return (
        await _execute(
            db,
            _CONTEXT,
            {
                "asset": asset,
                "number": number,
                "output": output,
                "lock": lock,
            },
        )
    ).one_or_none()


async def read_output(db, output, *, lock=False):
    return (await _execute(db, _OUTPUT, {"output": output, "lock": lock})).one_or_none()


async def lock_output_for_publish(db, asset, number, token, output):
    return (
        await _execute(
            db,
            _LOCK_OUTPUT,
            {
                "asset": asset,
                "number": number,
                "token": token,
                "output": output,
            },
        )
    ).one_or_none()


async def publish(
    db, asset, number, token, output, result, inventory, revision
) -> bool:
    return (
        await _scalar(
            db,
            _PUBLISH,
            {
                "asset": asset,
                "number": number,
                "token": token,
                "output": output,
                "result": result,
                "inventory": inventory,
                "revision": revision,
            },
        )
        is True
    )


async def fail(db, asset, number, token, error):
    await _execute(
        db, _FAIL, {"asset": asset, "number": number, "token": token, "error": error}
    )


async def invalidate(db, asset, number, output, error) -> bool:
    return (
        await _scalar(
            db,
            _INVALIDATE,
            {
                "asset": asset,
                "number": number,
                "output": output,
                "error": error,
            },
        )
        is True
    )


async def touch(db, asset, number, output):
    await _execute(db, _TOUCH, {"asset": asset, "number": number, "output": output})


async def expired(db):
    return (await _execute(db, _EXPIRED)).all()


async def stale(db, cutoff):
    return (await _execute(db, _STALE, {"cutoff": cutoff})).all()


async def remove_stale(db, asset, number, cutoff):
    await _execute(
        db, _REMOVE_STALE, {"asset": asset, "number": number, "cutoff": cutoff}
    )


async def retirable(db, cutoff):
    return (await _execute(db, _RETIRABLE, {"cutoff": cutoff})).scalars().all()


async def begin_retirement(db, output, cutoff):
    return (
        await _execute(db, _BEGIN_RETIREMENT, {"output": output, "cutoff": cutoff})
    ).one_or_none()


async def check_retirement(db, output, reference, permit):
    return await _scalar(
        db, _CHECK_RETIREMENT, {"output": output, "ref": reference, "permit": permit}
    )


async def finish_retirement(db, output, reference) -> bool:
    return (
        await _scalar(db, _FINISH_RETIREMENT, {"output": output, "ref": reference})
        is True
    )


async def retained_bytes(db) -> int:
    return await _scalar(db, _RETAINED)
