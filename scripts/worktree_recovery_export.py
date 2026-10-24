"""Synthetic export output retention and cache linkage in offline recovery.

The tiny ZIPs qualify physical relocation and immutable receipts, not the
application's export packaging or an actual source job's execution history.
"""

from datetime import timedelta
import hashlib
import io
import json
import uuid
import zipfile

STATES = ("pending", "ready", "retired")
CACHE_KEY = hashlib.sha256(b"aap:isolated-recovery-export-cache").hexdigest()


def identifier(kind, state):
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"aap:isolated-recovery-export:{kind}:{state}"
    )


def content_for(state):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("fixture.json", (2020, 1, 1, 0, 0, 0)),
            json.dumps({"fixture": "offline-recovery-export", "state": state}).encode(),
        )
    return stream.getvalue()


async def seed_export_outputs(db, *, project, storage, now):
    """Keep physical responsibility without creating matching source Job rows."""
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_output import ExportOutput
    from app.services import export_storage

    for state in STATES:
        raw = content_for(state)
        created = now - timedelta(days=8) if state == "retired" else now
        row = ExportOutput(
            id=identifier("output", state),
            project_id=project.id,
            job_id=identifier("absent-job", state),
            media="image",
            format="aap_json",
            file_count=1,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            created_at=created,
            expires_at=created + timedelta(days=7),
            state=state,
            retired_at=now if state == "retired" else None,
        )
        row.storage_ref = await export_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        owner = export_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType="application/zip",
        )
        if state == "ready":
            db.add(
                ExportArtifact(
                    cache_key=CACHE_KEY,
                    project_id=project.id,
                    batch_id=None,
                    format=row.format,
                    object_key=owner.object_key,
                    file_count=row.file_count,
                    size_bytes=row.size_bytes,
                    expires_at=row.expires_at,
                    output_id=row.id,
                )
            )
    await db.flush()


async def verify_export_outputs(db, *, storage):
    from sqlalchemy import select
    from app.db.models.export_artifact import ExportArtifact
    from app.db.models.export_output import ExportOutput
    from app.services import export_storage

    for state in STATES:
        row = await db.get(ExportOutput, identifier("output", state))
        raw = content_for(state)
        assert row is not None and row.state == state
        assert row.job_id == identifier("absent-job", state)
        assert row.expires_at - row.created_at == timedelta(days=7)
        assert (row.retired_at is not None) == (state == "retired")
        assert row.sha256 == hashlib.sha256(raw).hexdigest()
        assert row.size_bytes == len(raw) and row.file_count == 1
        owner = export_storage.resolve(row)
        assert owner.bucket == storage.export_bucket
        response = storage.client.get_object(Bucket=owner.bucket, Key=owner.object_key)
        try:
            assert response["Body"].read(row.size_bytes + 1) == raw
        finally:
            response["Body"].close()
        caches = list(
            await db.scalars(
                select(ExportArtifact).where(ExportArtifact.output_id == row.id)
            )
        )
        assert len(caches) == (1 if state == "ready" else 0)
        if caches:
            cache = caches[0]
            assert cache.cache_key == CACHE_KEY and cache.project_id == row.project_id
            assert (
                cache.object_key == owner.object_key
                and cache.expires_at == row.expires_at
            )
            assert (
                cache.size_bytes == row.size_bytes
                and cache.file_count == row.file_count
            )
            assert cache.format == row.format
    return {
        "pinned_export_outputs": 3,
        "verified_export_zip_objects": 3,
        "export_cache_links": 1,
    }
