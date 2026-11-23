"""Fixed export ledger calls over the business session, never the control session."""

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB


async def _execute(db, statement, params=None):
    if db.autoflush:
        await db.flush()
    return await db.execute(statement, params)


async def _row(db, sql, **params):
    return (await _execute(db, text(sql), params)).one_or_none()


async def _value(db, sql, **params):
    return (await _execute(db, text(sql), params)).scalar()


async def zip_receipt(db, identifier, *, mode="read"):
    return await _row(
        db,
        "SELECT * FROM yoao_export_v1.zip_receipt(:id, :mode)",
        id=identifier,
        mode=mode,
    )


async def reserve_zip(db, output):
    return (
        await _execute(
            db,
            text(
                "SELECT yoao_export_v1.reserve_zip(:id, :project_id, :job_id, "
                ":media_bundle_id, :media, :format, :file_count, :sha256, :size_bytes, "
                ":storage_ref, :created_at, :expires_at)"
            ).bindparams(bindparam("storage_ref", type_=JSONB)),
            vars(output),
        )
    ).scalar_one()


async def save_zip_proof(db, identifier, proof):
    return (
        await _execute(
            db,
            text("SELECT yoao_export_v1.save_zip_proof(:id, :proof)").bindparams(
                bindparam("proof", type_=JSONB(none_as_null=True))
            ),
            {"id": identifier, "proof": proof},
        )
    ).scalar_one()


async def publish_zip(db, identifier):
    return await _value(db, "SELECT yoao_export_v1.publish_zip(:id)", id=identifier)


async def allocate_media(db, bundle):
    return (
        await _execute(
            db,
            text(
                "SELECT * FROM yoao_export_v1.allocate_media(:id, :project_id, :job_id, "
                ":zip_output_id, :input_snapshot, :input_digest, :logical_paths, "
                ":inventory, :object_count, :size_bytes, :storage_ref, :created_at, :expires_at)"
            ).bindparams(
                bindparam("input_snapshot", type_=JSONB),
                bindparam("logical_paths", type_=JSONB),
                bindparam("inventory", type_=JSONB),
                bindparam("storage_ref", type_=JSONB),
            ),
            vars(bundle),
        )
    ).one_or_none()


async def media_delivery(db, identifier, *, lock=False):
    return await _row(
        db,
        "SELECT * FROM yoao_export_v1.media_delivery(:id, :lock)",
        id=identifier,
        lock=lock,
    )


async def media_fence(db, identifier, *, mode="read"):
    return await _row(
        db,
        "SELECT * FROM yoao_export_v1.media_fence(:id, :mode)",
        id=identifier,
        mode=mode,
    )


async def claim_media(db, identifier, token, expires):
    return await _value(
        db,
        "SELECT yoao_export_v1.claim_media(:id, :token, :expires)",
        id=identifier,
        token=token,
        expires=expires,
    )


async def publish_media(db, receipt, published):
    return await _value(
        db,
        "SELECT yoao_export_v1.publish_media(:id, :token, :expires, :published)",
        id=receipt.id,
        token=receipt.lease_token,
        expires=receipt.lease_expires_at,
        published=published,
    )


async def save_media_version(db, receipt, member_id, version):
    return await _value(
        db,
        "SELECT yoao_export_v1.save_media_version(:id, :member, :token, :expires, :version)",
        id=receipt.id,
        member=member_id,
        token=receipt.lease_token,
        expires=receipt.lease_expires_at,
        version=version,
    )


async def media_version(db, bundle_id, member_id):
    return await _value(
        db,
        "SELECT yoao_export_v1.media_version(:id, :member)",
        id=bundle_id,
        member=member_id,
    )


async def media_versions(db, bundle_id):
    return await _execute(
        db,
        text("SELECT member_id, version_id FROM yoao_export_v1.media_versions(:id)"),
        {"id": bundle_id},
    )


async def find_cache(db, key, now):
    return await _row(
        db, "SELECT * FROM yoao_export_v1.find_cache(:key, :now)", key=key, now=now
    )


async def lock_cache(db, artifact_id):
    return await _row(
        db, "SELECT * FROM yoao_export_v1.lock_cache(:id)", id=artifact_id
    )


async def invalidate_cache(db, artifact_id, output_id):
    await _execute(
        db,
        text("SELECT yoao_export_v1.invalidate_cache(:id, :output)"),
        {"id": artifact_id, "output": output_id},
    )


async def record_cache(db, key, output_id, batch_id):
    return await _row(
        db,
        "SELECT * FROM yoao_export_v1.record_cache(:key, :output, :batch)",
        key=key,
        output=output_id,
        batch=batch_id,
    )


async def media_attempt_ended(db, bundle_id, now):
    return await _value(
        db,
        "SELECT yoao_export_v1.media_attempt_ended(:id, :now)",
        id=bundle_id,
        now=now,
    )


async def media_has_operations(db, bundle_id, ignore):
    return await _value(
        db,
        "SELECT yoao_export_v1.media_has_operations(:id, :ignore)",
        id=bundle_id,
        ignore=ignore,
    )


async def lock_media_retirement(db, identifier, *, inventory):
    return await _row(
        db,
        "SELECT * FROM yoao_export_v1.lock_media_retirement(:id, :inventory)",
        id=identifier,
        inventory=inventory,
    )


async def start_media_retirement(db, identifier, now):
    return await _value(
        db,
        "SELECT yoao_export_v1.start_media_retirement(:id, :now)",
        id=identifier,
        now=now,
    )


async def finish_media_retirement(db, identifier, now):
    return await _value(
        db,
        "SELECT yoao_export_v1.finish_media_retirement(:id, :now)",
        id=identifier,
        now=now,
    )


async def media_retirement_queue(db, now, limit):
    return (
        (
            await _execute(
                db,
                text(
                    "SELECT * FROM yoao_export_v1.media_retirement_queue(:now, :limit)"
                ),
                {"now": now, "limit": limit},
            )
        )
        .scalars()
        .all()
    )


async def zip_retirement_bundle(db, identifier):
    return await _value(
        db, "SELECT yoao_export_v1.zip_retirement_bundle(:id)", id=identifier
    )


async def start_zip_retirement(db, identifier, now):
    return await _value(
        db,
        "SELECT yoao_export_v1.start_zip_retirement(:id, :now)",
        id=identifier,
        now=now,
    )


async def zip_retirement_unreferenced(db, identifier):
    return await _value(
        db, "SELECT yoao_export_v1.zip_retirement_unreferenced(:id)", id=identifier
    )


async def remove_retired_zip(db, identifier, now):
    return await _value(
        db,
        "SELECT yoao_export_v1.remove_retired_zip(:id, :now)",
        id=identifier,
        now=now,
    )


async def purge_legacy_cache(db, now, limit):
    return await _value(
        db,
        "SELECT yoao_export_v1.purge_legacy_cache(:now, :limit)",
        now=now,
        limit=limit,
    )


async def zip_retirement_queue(db, now, limit):
    return (
        (
            await _execute(
                db,
                text("SELECT * FROM yoao_export_v1.zip_retirement_queue(:now, :limit)"),
                {"now": now, "limit": limit},
            )
        )
        .scalars()
        .all()
    )


async def binding_history(db):
    row = await _row(db, "SELECT * FROM yoao_export_v1.binding_history()")
    return dict(row._mapping)
