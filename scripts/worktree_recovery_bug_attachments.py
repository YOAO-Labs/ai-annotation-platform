"""Preserve independent attachment responsibility and report linkage in recovery.

Synthetic image bytes prove relocation and immutable receipts. They do not claim
that legacy objects have acquired ownership or that pending uploads completed.
"""

from datetime import timedelta
import hashlib
import io
import uuid

STATES = ("pending", "ready", "retired")


def identifier(kind, state):
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"aap:isolated-recovery-bug-attachment:{kind}:{state}"
    )


def content_for(state):
    from PIL import Image

    stream = io.BytesIO()
    Image.new("RGB", (16, 12), (27, 51, 75 + STATES.index(state))).save(
        stream, format="PNG"
    )
    return stream.getvalue()


def metadata(row):
    from app.services import bug_attachment_storage

    return {
        "storageKey": bug_attachment_storage.resolve(row).object_key,
        "fileName": row.file_name,
        "mimeType": row.mime_type,
        "size": row.size_bytes,
    }


async def seed_bug_attachments(db, *, actor, storage, now):
    from app.db.models.bug_attachment_upload import BugAttachmentUpload
    from app.db.models.bug_report import BugReport
    from app.services import bug_attachment_storage

    ready = None
    for state in STATES:
        raw = content_for(state)
        created = now - timedelta(days=181) if state == "retired" else now
        row = BugAttachmentUpload(
            id=identifier("upload", state),
            owner_id=identifier("absent-actor", state)
            if state == "retired"
            else actor.id,
            file_name=f"recovery-{state}.png",
            mime_type="image/png",
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            created_at=created,
            expires_at=created + timedelta(days=180),
            state=state,
            retired_at=now if state == "retired" else None,
            bound_report_id=identifier("report", state) if state != "pending" else None,
        )
        row.storage_ref = await bug_attachment_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        owner = bug_attachment_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType=row.mime_type,
        )
        if state == "ready":
            ready = row
    entry = metadata(ready)
    db.add(
        BugReport(
            id=identifier("report", "ready"),
            display_id="BUG-RECOVERY-READY",
            reporter_id=actor.id,
            user_role=actor.role,
            route="/recovery",
            title="Synthetic ready attachment",
            description="Offline recovery linkage fixture",
            screenshot_url=entry["storageKey"],
            attachments=[entry],
        )
    )
    legacy_key = (
        f"bug-report-attachments/{actor.id}/{identifier('legacy', 'ready')}-legacy.png"
    )
    raw = content_for("ready")
    storage.client.put_object(
        Bucket=storage.bug_reports_bucket,
        Key=legacy_key,
        Body=raw,
        ContentType="image/png",
    )
    db.add(
        BugReport(
            id=identifier("legacy-report", "ready"),
            display_id="BUG-RECOVERY-LEGACY",
            reporter_id=actor.id,
            user_role=actor.role,
            route="/recovery",
            title="Synthetic legacy attachment",
            description="Unknown ownership remains unknown",
            screenshot_url=legacy_key,
            attachments=[
                {
                    "storageKey": legacy_key,
                    "fileName": "legacy.png",
                    "mimeType": "image/png",
                    "size": len(raw),
                }
            ],
        )
    )
    await db.flush()


async def verify_bug_attachments(db, *, storage):
    from sqlalchemy import select
    from app.db.models.bug_attachment_upload import BugAttachmentUpload
    from app.db.models.bug_report import BugReport
    from app.db.models.user import User
    from app.services import bug_attachment_storage

    for state in STATES:
        row = await db.get(BugAttachmentUpload, identifier("upload", state))
        raw = content_for(state)
        assert row is not None and row.state == state
        assert row.expires_at - row.created_at == timedelta(days=180)
        assert (row.retired_at is not None) == (state == "retired")
        assert row.sha256 == hashlib.sha256(raw).hexdigest() and row.size_bytes == len(
            raw
        )
        owner = bug_attachment_storage.resolve(row)
        assert owner.bucket == storage.bug_reports_bucket
        body = storage.client.get_object(Bucket=owner.bucket, Key=owner.object_key)[
            "Body"
        ]
        try:
            assert body.read(row.size_bytes + 1) == raw
        finally:
            body.close()
        if state == "ready":
            report = await db.get(BugReport, row.bound_report_id)
            assert report is not None and report.reporter_id == row.owner_id
            assert report.screenshot_url == owner.object_key and report.attachments == [
                metadata(row)
            ]
        elif state == "retired":
            assert row.bound_report_id == identifier("report", state)
            assert await db.get(BugReport, row.bound_report_id) is None
            assert await db.get(User, row.owner_id) is None
        else:
            assert row.bound_report_id is None
    legacy = await db.get(BugReport, identifier("legacy-report", "ready"))
    assert (
        legacy is not None
        and legacy.attachments[0]["storageKey"] == legacy.screenshot_url
    )
    assert (
        await db.scalar(
            select(BugAttachmentUpload.id).where(
                BugAttachmentUpload.storage_ref["object_key"].astext
                == legacy.screenshot_url
            )
        )
        is None
    )
    body = storage.client.get_object(
        Bucket=storage.bug_reports_bucket, Key=legacy.screenshot_url
    )["Body"]
    try:
        assert body.read(len(content_for("ready")) + 1) == content_for("ready")
    finally:
        body.close()
    return {
        "pinned_bug_attachments": 3,
        "verified_bug_attachment_images": 3,
        "bound_bug_attachment_reports": 1,
        "unowned_legacy_bug_attachments": 1,
    }
