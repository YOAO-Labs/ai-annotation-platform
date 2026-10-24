"""Retain generic comment files and their original ownership across recovery."""

from datetime import timedelta
import hashlib
import uuid

STATES = ("pending", "ready", "retired")


def identifier(kind, state):
    return uuid.uuid5(
        uuid.NAMESPACE_URL, f"aap:isolated-recovery-comment-attachment:{kind}:{state}"
    )


def content_for(state):
    # Empty ready files are an existing supported business input.
    return {
        "pending": b"pending\x00opaque-input",
        "ready": b"",
        "retired": b"%PDF-synthetic\n",
    }[state]


def metadata(row):
    from app.services import comment_attachment_storage

    return {
        "storageKey": comment_attachment_storage.resolve(row).object_key,
        "fileName": row.file_name,
        "mimeType": row.mime_type,
        "size": row.size_bytes,
    }


async def seed_comment_attachments(db, *, actor, project, storage, now):
    from sqlalchemy import select
    from app.db.models.annotation import Annotation
    from app.db.models.annotation_comment import AnnotationComment
    from app.db.models.comment_attachment_upload import CommentAttachmentUpload
    from app.services import comment_attachment_storage
    from app.services.feedback import FeedbackService

    annotation = await db.scalar(
        select(Annotation)
        .where(Annotation.project_id == project.id)
        .order_by(Annotation.id)
    )
    assert annotation is not None
    ready = None
    for state in STATES:
        raw = content_for(state)
        absent = state == "retired"
        created = now - timedelta(days=91) if absent else now
        row = CommentAttachmentUpload(
            id=identifier("upload", state),
            owner_id=identifier("absent-actor", state) if absent else actor.id,
            annotation_id=identifier("absent-annotation", state)
            if absent
            else annotation.id,
            task_id=identifier("absent-task", state) if absent else annotation.task_id,
            project_id=identifier("absent-project", state) if absent else project.id,
            file_name=f"recovery-{state}.bin",
            mime_type="application/octet-stream",
            sha256=hashlib.sha256(raw).hexdigest(),
            size_bytes=len(raw),
            created_at=created,
            expires_at=created + timedelta(hours=2160),
            state=state,
            retired_at=now if absent else None,
            bound_comment_id=identifier("comment", state)
            if state != "pending"
            else None,
        )
        row.storage_ref = await comment_attachment_storage.allocate(db, row)
        db.add(row)
        await db.flush()
        owner = comment_attachment_storage.resolve(row)
        storage.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType=row.mime_type,
        )
        if state == "ready":
            ready = row
    comment = AnnotationComment(
        id=identifier("comment", "ready"),
        annotation_id=annotation.id,
        project_id=project.id,
        author_id=actor.id,
        body="Synthetic empty file with independent ownership",
        attachments=[metadata(ready)],
    )
    db.add(comment)
    await db.flush()
    await FeedbackService(db).mirror_annotation_comment(
        comment, task_id=annotation.task_id
    )
    legacy_key = f"comment-attachments/{annotation.id}/{identifier('legacy', 'ready')}-legacy.dat"
    raw = b"legacy bytes without location evidence"
    storage.client.put_object(Bucket=storage.bucket, Key=legacy_key, Body=raw)
    db.add(
        AnnotationComment(
            id=identifier("legacy-comment", "ready"),
            annotation_id=annotation.id,
            project_id=project.id,
            author_id=actor.id,
            body="Historical location stays unknown",
            attachments=[
                {
                    "storageKey": legacy_key,
                    "fileName": "legacy.dat",
                    "mimeType": "application/octet-stream",
                    "size": len(raw),
                }
            ],
        )
    )
    await db.flush()


async def verify_comment_attachments(db, *, storage):
    from sqlalchemy import select
    from app.db.models.annotation import Annotation
    from app.db.models.annotation_comment import AnnotationComment
    from app.db.models.annotation_feedback import AnnotationFeedback
    from app.db.models.comment_attachment_upload import CommentAttachmentUpload
    from app.db.models.user import User
    from app.services import comment_attachment_storage

    for state in STATES:
        row = await db.get(CommentAttachmentUpload, identifier("upload", state))
        raw = content_for(state)
        assert row is not None and row.state == state
        assert row.expires_at - row.created_at == timedelta(hours=2160)
        assert (row.retired_at is not None) == (state == "retired")
        assert row.sha256 == hashlib.sha256(raw).hexdigest() and row.size_bytes == len(
            raw
        )
        owner = comment_attachment_storage.resolve(row)
        assert owner.bucket == storage.bucket
        assert comment_attachment_storage.read(storage.client, row) == raw
        if state == "ready":
            original = await db.get(AnnotationComment, row.bound_comment_id)
            assert original is not None and original.author_id == row.owner_id
            assert (
                original.annotation_id == row.annotation_id
                and original.project_id == row.project_id
            )
            assert original.attachments == [metadata(row)]
            mirror = await db.scalar(
                select(AnnotationFeedback).where(
                    AnnotationFeedback.annotation_id == row.annotation_id,
                    AnnotationFeedback.body == original.body,
                    AnnotationFeedback.kind == "comment",
                )
            )
            assert mirror is not None and mirror.attachments == original.attachments
        elif state == "retired":
            assert row.bound_comment_id == identifier("comment", state)
            assert await db.get(AnnotationComment, row.bound_comment_id) is None
            assert await db.get(Annotation, row.annotation_id) is None
            assert await db.get(User, row.owner_id) is None
        else:
            assert row.bound_comment_id is None
    legacy = await db.get(AnnotationComment, identifier("legacy-comment", "ready"))
    assert legacy is not None
    key = legacy.attachments[0]["storageKey"]
    assert (
        await db.scalar(
            select(CommentAttachmentUpload.id).where(
                CommentAttachmentUpload.storage_ref["object_key"].astext == key
            )
        )
        is None
    )
    body = storage.client.get_object(Bucket=storage.bucket, Key=key)["Body"]
    try:
        assert body.read(1024) == b"legacy bytes without location evidence"
    finally:
        body.close()
    return {
        "pinned_comment_attachments": 3,
        "verified_comment_attachment_files": 3,
        "bound_comment_attachment_comments": 1,
        "metadata_only_comment_attachment_mirrors": 1,
        "unowned_legacy_comment_attachments": 1,
    }
