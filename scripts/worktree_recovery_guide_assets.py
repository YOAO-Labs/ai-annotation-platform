"""Preserve guide-image owners, current references and unknown historical bytes."""

from datetime import timedelta
import hashlib
import io
import uuid

CASES = (
    ("png", "pending", "image/png", "PNG"),
    ("jpeg", "ready", "image/jpeg", "JPEG"),
    ("webp", "ready", "image/webp", "WEBP"),
    ("gif", "retired", "image/gif", "GIF"),
    ("svg", "ready", "image/svg+xml", None),
)
LEGACY_BYTES = (
    b'<svg xmlns="http://www.w3.org/2000/svg"><text>unknown origin</text></svg>'
)


def identifier(kind, case="shared"):
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"aap:isolated-recovery-guide-asset:{kind}:{case}"
    )


def content_for(case):
    if case == "svg":
        return b'<svg xmlns="http://www.w3.org/2000/svg" width="2" height="2"><rect width="2" height="2" fill="blue"/></svg>\n'
    from PIL import Image

    image_format = next(item[3] for item in CASES if item[0] == case)
    with io.BytesIO() as output, Image.new("RGB", (2, 2), "blue") as image:
        image.save(output, format=image_format)
        return output.getvalue()


def legacy_entry(project_id, created_at):
    from app.services import guide_asset_storage

    return {
        "key": guide_asset_storage.key_for(project_id, identifier("legacy")),
        "original_name": "unknown-origin.svg",
        "content_type": "image/svg+xml",
        "size": len(LEGACY_BYTES),
        "uploaded_at": created_at.isoformat().replace("+00:00", "Z"),
    }


async def seed_guide_assets(db, *, actor, project, storage, now):
    from app.db.models.guide_asset_upload import GuideAssetUpload
    from app.db.models.project import Project
    from app.db.models.project_guide_asset import ProjectGuideAsset
    from app.db.models.user import User
    from app.services import guide_asset_storage

    assert not project.guide_assets
    original_actor = User(
        id=identifier("original-actor"),
        email="recovery-guide-original@example.test",
        name="Synthetic deleted guide uploader",
        password_hash="synthetic-unused-authentication",
        role="employee",
    )
    db.add(original_actor)
    await db.flush()
    original_project = Project(
        id=identifier("original-project"),
        display_id="P-REC-GUIDE-ORIGIN",
        name="Synthetic deleted guide origin",
        type_key="image-det",
        type_label="Image detection",
        owner_id=original_actor.id,
    )
    copied_project = Project(
        id=identifier("current-copy"),
        display_id="P-REC-GUIDE-COPY",
        name="Synthetic surviving guide copy",
        type_key="image-det",
        type_label="Image detection",
        owner_id=actor.id,
    )
    db.add_all([original_project, copied_project])
    await db.flush()
    owners = {}
    for case, state, mime_type, _ in CASES:
        raw = content_for(case)
        row = GuideAssetUpload(
            id=identifier("upload", case),
            owner_id=original_actor.id if case == "svg" else actor.id,
            project_id=original_project.id if case == "svg" else project.id,
            file_name=f"recovery-guide.{case}",
            mime_type=mime_type,
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            created_at=now - timedelta(days=2),
            state=state,
            retired_at=now - timedelta(days=1) if state == "retired" else None,
            last_attempted_at=now - timedelta(hours=1),
        )
        row.storage_ref = await guide_asset_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        owner = guide_asset_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType=row.mime_type,
        )
        owners[case] = row
    # Current references, not original project prefixes, retain the shared SVG.
    for target, cases in (
        (project, ("jpeg", "webp", "svg")),
        (copied_project, ("svg",)),
    ):
        for case in cases:
            db.add(
                ProjectGuideAsset(
                    project_id=target.id, upload_id=owners[case].id, created_at=now
                )
            )
        target.guide_assets = [
            guide_asset_storage.entry(owners[case]) for case in cases
        ]
    legacy = legacy_entry(project.id, owners["svg"].created_at)
    project.guide_assets = [*project.guide_assets, legacy]
    storage.client.put_object(
        Bucket=storage.bucket,
        Key=legacy["key"],
        Body=LEGACY_BYTES,
        ContentType="image/svg+xml",
    )
    await db.flush()
    # These rows really existed. Deletion must leave independent owner snapshots.
    await db.delete(original_project)
    await db.flush()
    await db.delete(original_actor)
    await db.flush()


async def verify_guide_assets(db, *, storage):
    from sqlalchemy import select, text
    from app.db.models.guide_asset_upload import GuideAssetUpload
    from app.db.models.project import Project
    from app.db.models.project_guide_asset import ProjectGuideAsset
    from app.db.models.user import User
    from app.services import guide_asset_storage

    owners = {}
    for case, state, mime_type, _ in CASES:
        row = await db.get(GuideAssetUpload, identifier("upload", case))
        raw = content_for(case)
        assert row is not None and row.state == state
        assert row.mime_type == mime_type and row.file_name == f"recovery-guide.{case}"
        assert row.sha256 == hashlib.sha256(raw).hexdigest()
        assert row.size_bytes == len(raw)
        assert row.last_attempted_at - row.created_at == timedelta(hours=47)
        if state == "retired":
            assert row.retired_at - row.created_at == timedelta(days=1)
        else:
            assert row.retired_at is None
        owner = guide_asset_storage.resolve(row)
        assert owner.bucket == storage.bucket
        assert owner.object_key == guide_asset_storage.key_for(row.project_id, row.id)
        assert guide_asset_storage.read(storage.client, row) == raw
        owners[case] = row
    shared = owners["svg"]
    assert shared.owner_id == identifier("original-actor")
    assert shared.project_id == identifier("original-project")
    assert await db.get(User, shared.owner_id) is None
    assert await db.get(Project, shared.project_id) is None
    original = await db.get(Project, owners["jpeg"].project_id)
    copy = await db.get(Project, identifier("current-copy"))
    assert original is not None and copy is not None
    assert await db.get(User, original.owner_id) is not None
    assert original.owner_id == copy.owner_id
    for case in ("png", "jpeg", "webp", "gif"):
        assert owners[case].project_id == original.id
        assert owners[case].owner_id == original.owner_id
    expected_refs = {
        (original.id, owners["jpeg"].id),
        (original.id, owners["webp"].id),
        (original.id, shared.id),
        (copy.id, shared.id),
    }
    refs = list(
        await db.scalars(
            select(ProjectGuideAsset).where(
                ProjectGuideAsset.upload_id.in_([row.id for row in owners.values()])
            )
        )
    )
    assert {(row.project_id, row.upload_id) for row in refs} == expected_refs
    assert all(row.created_at - shared.created_at == timedelta(days=2) for row in refs)
    legacy = legacy_entry(original.id, shared.created_at)
    assert original.guide_assets == [
        guide_asset_storage.entry(owners[case]) for case in ("jpeg", "webp", "svg")
    ] + [legacy]
    assert copy.guide_assets == [guide_asset_storage.entry(shared)]
    revisions = dict(
        (
            await db.execute(
                text(
                    "SELECT id, guide_assets_revision FROM projects "
                    "WHERE id IN (:original, :copy)"
                ),
                {"original": original.id, "copy": copy.id},
            )
        ).all()
    )
    assert revisions == {original.id: 2, copy.id: 2}
    assert await db.get(GuideAssetUpload, identifier("legacy")) is None
    assert (
        await db.scalar(
            select(GuideAssetUpload.id).where(
                GuideAssetUpload.storage_ref["object_key"].astext == legacy["key"]
            )
        )
        is None
    )
    body = storage.client.get_object(Bucket=storage.bucket, Key=legacy["key"])["Body"]
    try:
        assert body.read(len(LEGACY_BYTES) + 1) == LEGACY_BYTES
        assert body.read(1) == b""
    finally:
        body.close()
    return {
        "pinned_guide_assets": len(CASES),
        "verified_guide_asset_files": len(CASES),
        "guide_asset_project_refs": len(refs),
        "shared_guide_asset_projects": 2,
        "guide_asset_missing_original_projects": 1,
        "guide_asset_missing_uploaders": 1,
        "guide_asset_protected_project_revisions": len(revisions),
        "unowned_legacy_guide_assets": 1,
    }
