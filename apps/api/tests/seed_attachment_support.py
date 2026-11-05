"""Owner-level seed integration helpers using real PostgreSQL and HTTP.

The object provider is explicitly controlled; these helpers do not establish
restricted-role permissions or exercise real S3.
"""

import hashlib
import io
from types import SimpleNamespace
import uuid

from botocore.exceptions import ClientError

from app.db.models.comment_attachment_upload import CommentAttachmentUpload as Upload


class Objects:
    """Fake only the object provider; all HTTP, locks and owner SQL are real."""

    def __init__(self, original):
        for name in (
            "bucket",
            "datasets_bucket",
            "bug_reports_bucket",
            "media_cache_bucket",
            "audit_archive_bucket",
            "import_bucket",
            "export_bucket",
            "avatars_bucket",
            "training_bucket",
        ):
            setattr(self, name, getattr(original, name))
        self.client = self
        self.meta = SimpleNamespace(endpoint_url=original.client.meta.endpoint_url)
        self.data, self.deleted = {}, []
        self.fail_delete = False
        self.body_factory = io.BytesIO
        self.bodies = []
        self.probe = None

    def network(self):
        if self.probe is not None:
            self.probe()

    def put_object(self, *, Bucket, Key, Body=b"", **kwargs):  # noqa: N803
        self.network()
        if kwargs.get("IfNoneMatch") == "*" and (Bucket, Key) in self.data:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.data[Bucket, Key] = Body.read() if hasattr(Body, "read") else bytes(Body)
        return {}

    def head_object(self, *, Bucket, Key):  # noqa: N803
        self.network()
        if (Bucket, Key) not in self.data:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "HeadObject")
        data = self.data[Bucket, Key]
        return {
            "ContentLength": len(data),
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
        }

    def get_object(self, *, Bucket, Key, IfMatch, **kwargs):  # noqa: N803
        self.network()
        metadata = self.head_object(Bucket=Bucket, Key=Key)
        assert metadata["ETag"] == IfMatch
        body = self.body_factory(self.data[Bucket, Key])
        self.bodies.append(body)
        return {"Body": body, **metadata}

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self.network()
        if self.fail_delete:
            raise OSError("private provider failure")
        self.data.pop((Bucket, Key), None)
        self.deleted.append((Bucket, Key))
        return {}

    def delete_objects(self, *, Bucket, Delete):  # noqa: N803
        self.network()
        for item in Delete["Objects"]:
            self.delete_object(Bucket=Bucket, Key=item["Key"])
        return {}

    def list_objects_v2(self, *, Bucket, Prefix, **kwargs):  # noqa: N803
        self.network()
        values = [
            {"Key": key}
            for bucket, key in sorted(self.data)
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return {
            "Contents": values[: kwargs.get("MaxKeys", len(values))],
            "IsTruncated": False,
        }


async def build(ctx):
    name = "sa" + uuid.uuid4().hex[:8]
    run = ctx.suite.new_run(name)
    response = await ctx.suite.owner.post(
        "/api/v1/__test/seed/owned", json=run.payload()
    )
    assert response.status_code == 200, response.text
    ctx.runs.append(run)
    world = response.json()
    login = await ctx.suite.owner.post(
        "/api/v1/__test/seed/login", json={"email": world["admin_email"]}
    )
    assert login.status_code == 200, login.text
    world.update(
        namespace=name,
        run=run,
        actor_id=login.json()["user"]["id"],
        headers={"Authorization": "Bearer " + login.json()["access_token"]},
    )
    response = await ctx.http.post(
        f"/api/v1/tasks/{world['task_ids'][0]}/annotations",
        headers=world["headers"],
        json={
            "annotation_type": "bbox",
            "tool_unit_id": "bbox",
            "class_name": "car",
            "geometry": {"type": "bbox", "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
        },
    )
    assert response.status_code == 201, response.text
    world["annotation_id"] = response.json()["id"]
    return world


async def upload(ctx, world, raw=b"generic\x00bytes", *, bind=True, actor=None):
    headers = (actor or world)["headers"]
    response = await ctx.http.post(
        f"/api/v1/annotations/{world['annotation_id']}/comment-attachments/upload",
        params={"file_name": "general.dat"},
        headers={**headers, "Content-Type": "application/octet-stream"},
        content=raw,
    )
    assert response.status_code == 200, response.text
    attachment = response.json()
    comment_id = None
    if bind:
        response = await ctx.http.post(
            f"/api/v1/annotations/{world['annotation_id']}/comments",
            headers=headers,
            json={"body": "", "attachments": [attachment]},
        )
        assert response.status_code == 201, response.text
        comment_id = response.json()["id"]
    row = await ctx.db.get(Upload, uuid.UUID(attachment["storageKey"].split("/")[-1]))
    await ctx.db.commit()
    return row, attachment, comment_id
