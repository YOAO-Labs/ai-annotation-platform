"""Portable accepted-input facts for owner/client tests, with no storage effects."""

from datetime import datetime, timezone
import hashlib
from uuid import uuid4

from aap_assets_inputs import contract as c

BODY = b"accepted input bytes"


def input_work(*, status="ready"):
    input_id, job_id, task_id, project_id, actor_id = (uuid4() for _ in range(5))
    source = c.ImageSource(
        owner=c.ImageOwner(kind="task", id=task_id),
        task_id=task_id,
        project_id=project_id,
        dataset_id=None,
        item_id=None,
        source_generation=1,
        source_snapshot="a" * 64,
        object_identity=c.ObjectIdentity(etag="source-etag", byte_size=len(BODY)),
        source_drifted=False,
        upload_ready=True,
        file_path="private/original.jpg",
        file_name="original.jpg",
        file_type="image",
        bucket_kind="legacy",
        file_size=len(BODY),
        content_hash=None,
        width=20,
        height=10,
        thumbnail_path=None,
        blurhash=None,
        thumbnail_source_generation=None,
        thumbnail_error=None,
    )
    measured = status in {"ready", "expired"}
    record = c.InputRecord(
        input_id=input_id,
        job_id=job_id,
        task_id=task_id,
        project_id=project_id,
        actor_id=actor_id,
        status=status,
        task_version=1,
        task_source_generation=1,
        dataset_item_id=None,
        task_source_snapshot="b" * 64,
        source_owner=source.owner,
        source_generation=1,
        source_snapshot=source.source_snapshot,
        byte_size=len(BODY) if measured else None,
        sha256=hashlib.sha256(BODY).hexdigest() if measured else None,
        error="source_changed" if status == "failed" else None,
        created_at=datetime.now(timezone.utc),
        retire_after=None,
    )
    blob = (
        c.InputBlob(
            key=c.attempt_prefix(input_id, 1, uuid4()) + "source",
            version_id="version-a",
            etag="input-etag",
            byte_size=len(BODY),
            sha256=hashlib.sha256(BODY).hexdigest(),
        )
        if measured
        else None
    )
    return c.InputWork(input=record, source=source, blob=blob)


def input_request(work):
    return c.InputRequest(
        input_id=work.input.input_id,
        job_id=work.input.job_id,
        task_id=work.input.task_id,
    )


def input_reference(work):
    return c.InputReference(
        input=work.input,
        url=f"http://s3.test/inputs/{work.blob.key}?versionId={work.blob.version_id}",
        version_id=work.blob.version_id,
        expires_in=900,
    )
