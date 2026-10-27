"""Exercise retained-file readers against an owned, offline-source migration.

Avatar and RLE use durable application owners. The other fixtures qualify their
physical read seams; their authorization and owner bookends have SQL regressions.
"""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import uuid
from urllib.request import urlopen

from storage_s3_contract import require
from verify_storage_migration_artifact_reads import ArtifactReads
from verify_storage_migration_finalization import ROOT, operator, private_write


def packed(value):
    if isinstance(value, uuid.UUID):
        return {"uuid": str(value)}
    if isinstance(value, datetime):
        return {"datetime": value.isoformat()}
    if type(value) is date:
        return {"date": value.isoformat()}
    if isinstance(value, bytes):
        return {"bytes": base64.b64encode(value).decode()}
    if isinstance(value, dict):
        return {key: packed(item) for key, item in value.items()}
    if isinstance(value, list):
        return [packed(item) for item in value]
    return value


def unpacked(value):
    if isinstance(value, dict):
        if set(value) == {"uuid"}:
            return uuid.UUID(value["uuid"])
        if set(value) == {"datetime"}:
            return datetime.fromisoformat(value["datetime"])
        if set(value) == {"date"}:
            return date.fromisoformat(value["date"])
        if set(value) == {"bytes"}:
            return base64.b64decode(value["bytes"], validate=True)
        return {key: unpacked(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpacked(item) for item in value]
    return value


async def seed_retained(input_path, output_path):
    from PIL import Image
    from sqlalchemy import select, text
    from app.db.models.avatar_upload import AvatarUpload
    from app.db.models.raster_mask_object import RasterMaskObject
    from app.services import (
        audit_archive_storage as audit,
        avatar,
        avatar_image,
        avatar_storage,
        bug_attachment_storage as bug,
        comment_attachment_storage as comment,
        export_media_storage as media,
        guide_asset_storage as guide,
        raster_mask_objects as masks,
        raster_object_storage as raster,
    )
    from app.services.raster_mask_storage import (
        build_rle_gzip_reference,
        canonical_rle_bytes_gzip,
    )
    from app.services.storage import storage_service
    from app.workers._db import task_session

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    created = datetime.now(timezone.utc)
    actor_id = uuid.UUID(value["actor_id"])
    image = io.BytesIO()
    Image.new("RGB", (64, 48), (47, 103, 167)).save(image, format="PNG")
    png = image.getvalue()
    fixtures = []
    async with task_session() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == value["database"],
            "seed_database_changed",
        )
        token = avatar.new_upload_token()
        webp = avatar_image.normalize_avatar(png)
        upload = AvatarUpload(
            token=token,
            owner_id=actor_id,
            storage_ref=await avatar_storage.allocate(db, token),
            sha256=hashlib.sha256(webp).hexdigest(),
            size_bytes=len(webp),
            state="pending",
        )
        db.add(upload)
        await db.commit()
        upload.object_version_id = avatar.write_avatar_object(upload, webp)
        upload.state = "ready"
        await db.commit()
        fixtures.append(
            {
                "family": "avatar",
                "record": {
                    name: getattr(upload, name)
                    for name in (
                        "token",
                        "storage_ref",
                        "sha256",
                        "size_bytes",
                        "object_version_id",
                    )
                },
                "raw": webp,
            }
        )
        for family, storage in (("bug", bug), ("comment", comment), ("guide", guide)):
            record = SimpleNamespace(
                id=uuid.uuid4(),
                owner_id=actor_id,
                project_id=uuid.uuid4(),
                annotation_id=uuid.uuid4(),
                task_id=uuid.uuid4(),
                bound_comment_id=None,
                mime_type="image/png",
                file_name="owned.png",
                created_at=created,
                expires_at=created + timedelta(days=7),
                sha256=hashlib.sha256(png).hexdigest(),
                size_bytes=len(png),
                object_version_id=None,
            )
            record.storage_ref = await storage.allocate(db, record)
            if family == "comment":
                record.expires_at = created + timedelta(
                    seconds=comment.RETENTION_SECONDS
                )
            owner = storage.resolve(record)
            response = storage_service.client.put_object(
                Bucket=owner.bucket,
                Key=owner.object_key,
                Body=png,
                ContentType="image/png",
            )
            if family == "bug":
                record.object_version_id = response["VersionId"]
            fixtures.append({"family": family, "record": vars(record), "raw": png})
        record = SimpleNamespace(
            id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            year=2026,
            month=1,
            source_schema="public",
            source_table="audit_logs_y2026m01",
            parent_oid=100,
            source_oid=101,
            month_start=date(2026, 1, 1),
            month_end=date(2026, 2, 1),
            format_revision=1,
            row_count=1,
            created_at=created,
            state="ready",
            published_at=created,
        )
        line = audit.canonical_line(
            {
                **dict.fromkeys(audit._ROW_FIELDS),
                "id": 1,
                "created_at": datetime(2026, 1, 2, tzinfo=timezone.utc),
                "action": "owned.read",
            }
        )
        record.source_size_bytes, record.source_sha256 = (
            len(line),
            hashlib.sha256(line).hexdigest(),
        )
        record.storage_ref = await audit.allocate(db, record)
        raw = gzip.compress(line + audit.encode_meta(record), mtime=0)
        record.sha256, record.size_bytes = hashlib.sha256(raw).hexdigest(), len(raw)
        owner = audit.resolve(record)
        storage_service.client.put_object(
            Bucket=owner.bucket,
            Key=owner.object_key,
            Body=raw,
            ContentType="application/gzip",
        )
        fixtures.append({"family": "audit", "record": vars(record), "raw": raw})
        rle = {"encoding": "coco_rle", "size": [24, 32], "counts": [0, 1, 767]}
        reference = build_rle_gzip_reference(rle)
        _, raw = canonical_rle_bytes_gzip(rle)
        await masks.store(reference, raw, db=db)
        await db.commit()
        row = await db.scalar(
            select(RasterMaskObject).where(
                RasterMaskObject.logical_key == reference["object_key"]
            )
        )
        snapshot = raster.snapshot(row)
        fixtures.append(
            {
                "family": "raster",
                "record": {name: getattr(snapshot, name) for name in raster._FACTS}
                | {"storage_ref": snapshot.storage_ref},
                "raw": raw,
                "reference": reference,
                "rle": rle,
            }
        )
        identifier, relative = uuid.uuid4(), str(uuid.uuid4()) + ".bin"
        record = SimpleNamespace(
            id=identifier,
            created_at=created,
            expires_at=created + timedelta(days=7),
            input_schema=1,
        )
        record.storage_ref = media.make_ref(identifier, bucket=value["export_bucket"])
        owner = media.resolve(record)
        raw = b"owned-independent-media"
        record.inventory = {
            "schema": 1,
            "objects": {
                owner.key(relative): {
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "size_bytes": len(raw),
                }
            },
        }
        storage_service.client.put_object(
            Bucket=owner.bucket,
            Key=owner.key(relative),
            Body=raw,
            ContentType="application/octet-stream",
        )
        fixtures.append(
            {
                "family": "media",
                "record": vars(record),
                "relative": relative,
                "raw": raw,
            }
        )
        await db.commit()
        private_write(
            output_path,
            json.dumps(
                packed(
                    {
                        "fixtures": fixtures,
                        "avatar_token": token,
                        "avatar_sha256": upload.sha256,
                    }
                )
            ).encode(),
        )


async def worker_read(fixture_path, *, hold=False):
    from app.db.models.avatar_upload import AvatarUpload
    from app.db.models.raster_mask_object import RasterMaskObject
    from app.services import (
        audit_archive_storage as audit,
        avatar_uploads,
        bug_attachment_storage as bug,
        comment_attachment_storage as comment,
        export_media_storage as media,
        guide_asset_storage as guide,
        raster_mask_objects as masks,
        raster_object_storage as raster,
        storage_migration_artifact_reads as reads,
        storage_migration_read_publications as publications,
    )
    from app.services.content_snapshot import ContentError
    from app.workers._db import task_session

    value = unpacked(json.loads(Path(fixture_path).read_bytes()))
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    verified, media_url = [], None
    async with task_session() as db:

        async def durable_receipts():
            result = {}
            for fixture in value["fixtures"]:
                record = fixture["record"]
                if fixture["family"] == "avatar":
                    row = await db.get(
                        AvatarUpload, record["token"], populate_existing=True
                    )
                    require(
                        row is not None and row.state == "ready", "avatar_owner_changed"
                    )
                    result["avatar"] = packed(vars(avatar_uploads._snapshot(row)))
                elif fixture["family"] == "raster":
                    row = await db.get(
                        RasterMaskObject, record["id"], populate_existing=True
                    )
                    require(
                        row is not None and row.state == "ready", "raster_owner_changed"
                    )
                    snapshot = raster.snapshot(row)
                    result["raster"] = packed(
                        {name: getattr(snapshot, name) for name in raster._FACTS}
                        | {"storage_ref": snapshot.storage_ref}
                    )
            return result

        durable_before = await durable_receipts()
        for fixture in value["fixtures"]:
            family, record = fixture["family"], SimpleNamespace(**fixture["record"])
            if hold and family != "comment":
                continue
            before = packed(vars(record))
            ref = record.storage_ref
            key = ref.get("object_key")
            raw = fixture["raw"]
            if family == "media":
                key = media.resolve(record).key(fixture["relative"])
            route = await reads.resolve(
                db,
                ref,
                key,
                hashlib.sha256(raw).hexdigest(),
                len(raw),
                source_version_id=(
                    record.object_version_id
                    if family in {"avatar", "bug"}
                    and record.object_version_id is not None
                    else publications.ANY_VERSION
                ),
            )
            require(route is not None, "retained_route_missing")
            if family == "avatar":
                data, _ = await avatar_uploads.read(db, record.token)
                require(data == raw, "avatar_changed")
            elif family == "raster":
                require(
                    await masks.load(fixture["reference"], db=db) == fixture["rle"],
                    "raster_changed",
                )
            else:

                def read(client):
                    if hold:

                        def pause(http_response, parsed, **kwargs):
                            body = parsed.get("Body")
                            if body is None:
                                return
                            private_write(value["read_started"], b"owned")
                            deadline = time.monotonic() + 45
                            try:
                                while not Path(value["read_continue"]).exists():
                                    require(
                                        time.monotonic() < deadline, "read_hold_timeout"
                                    )
                                    time.sleep(0.05)
                            except BaseException:
                                body.close()
                                raise

                        client.meta.events.register("after-call.s3.GetObject", pause)
                    if family == "bug":
                        return bug.read_from_client(
                            client, record, response_version_id=route.target_version_id
                        )
                    if family == "audit":
                        return audit.read_from_client(client, record)
                    if family in {"comment", "guide"}:
                        return {"comment": comment, "guide": guide}[family].read_routed(
                            client, record
                        )
                    media.verify_read(client, record, fixture["relative"])
                    return media.sign_read(client, record, fixture["relative"])

                try:
                    result = await reads.run(
                        db, route, "owned_retained_" + family, read
                    )
                except ContentError:
                    require(
                        hold and Path(value["read_started"]).is_file(),
                        "unexpected_retained_denial",
                    )
                    return {"denied_after_withdrawal": True}
                require(not hold, "withdrawn_read_delivered")
                if family == "audit":
                    require(
                        result["total"] == 2
                        and result["items"][0]["action"] == "owned.read",
                        "audit_semantics_changed",
                    )
                elif family == "media":
                    require(
                        await publications.prefix_inventory(
                            db, route, prefix=media.resolve(record).prefix
                        )
                        == record.inventory["objects"],
                        "media_prefix_changed",
                    )
                    media_url = result
                else:
                    require(result == raw, "retained_bytes_changed")
            require(packed(vars(record)) == before, "saved_receipt_changed")
            verified.append(family)
        require(not hold and len(verified) == 7, "retained_read_set_incomplete")
        require(await durable_receipts() == durable_before, "durable_receipts_changed")
        return {
            "verified": verified,
            "media_url": media_url,
            "saved_receipts_unchanged": True,
        }


class RetainedReads(ArtifactReads):
    def child(self, owner, environment, invocation):
        from textwrap import indent

        failure = owner.private / ("retained-failure-" + uuid.uuid4().hex + ".json")
        guarded = (
            "try:\n" + indent(invocation, "    ") + "\nexcept BaseException as exc:\n"
            "    import json,traceback\n"
            "    from pathlib import Path\n"
            "    from verify_storage_migration_finalization import private_write\n"
            "    details={'type':type(exc).__name__,'frames':[[Path(frame.filename).name,frame.lineno] for frame in traceback.extract_tb(exc.__traceback__)]}\n"
            + f"    private_write({str(failure)!r},json.dumps(details).encode())\n"
            "    raise\n"
        )
        try:
            return super().child(owner, environment, guarded)
        except BaseException:
            if failure.is_file():
                owner.data["retained_failure"] = json.loads(failure.read_bytes())
                owner.save()
            raise

    def prepare(self, owner, environment, seed, source, buckets):
        from sqlalchemy.engine import make_url

        input_path, output_path = (
            owner.private / "retained-input.json",
            owner.private / "retained-seed.json",
        )
        private_write(
            input_path,
            json.dumps(
                {
                    "database": make_url(environment["DATABASE_URL"]).database,
                    "actor_id": seed["actor_id"],
                    "export_bucket": buckets["export"],
                }
            ).encode(),
        )
        self.child(
            owner,
            environment,
            "from verify_storage_migration_retained_reads import seed_retained\n"
            + f"asyncio.run(seed_retained({str(input_path)!r},{str(output_path)!r}))",
        )
        self.value = json.loads(output_path.read_bytes())
        self.value.update(
            read_started=str(owner.private / "retained-get-started"),
            read_continue=str(owner.private / "retained-get-continue"),
        )
        self.fixture_path = owner.private / "retained-worker.json"
        private_write(self.fixture_path, json.dumps(self.value).encode())
        name = "aap_owned_retained_" + owner.data["nonce"]
        self.task_name, self.hold_task_name = name + ".read", name + ".hold"
        self.worker_app = name + ":celery_app"
        private_write(
            owner.private / (name + ".py"),
            (
                "import asyncio,sys\n"
                + f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from app.workers.celery_app import celery_app\nfrom verify_storage_migration_retained_reads import worker_read\n"
                + f"@celery_app.task(name={self.task_name!r})\ndef read(): return asyncio.run(worker_read({str(self.fixture_path)!r}))\n"
                + f"@celery_app.task(name={self.hold_task_name!r})\ndef hold(): return asyncio.run(worker_read({str(self.fixture_path)!r},hold=True))\n"
            ).encode(),
        )
        environment["PYTHONPATH"] = (
            str(owner.private) + os.pathsep + str(ROOT / "apps/api")
        )

    def after_seal(self, **context):
        owner, http, summary = (context[key] for key in ("owner", "http", "summary"))

        def op(action, **flags):
            return operator(
                owner,
                context["native_environment"],
                action,
                context["seed"]["actor_id"],
                context["finalization_id"],
                context["config"],
                context["state"],
                **flags,
            )

        publication = op(
            "publish-reads", expected_revision=context["sealed"]["revision"]
        )["item"]
        require(publication["state"] == "active", "retained_not_published")
        value = unpacked(self.value)
        keys = {
            fixture["record"]["storage_ref"].get("object_key")
            for fixture in value["fixtures"]
        }
        for fixture in value["fixtures"]:
            if fixture["family"] == "media":
                keys.update(fixture["record"]["inventory"]["objects"])
        matches = [row for row in context["final_rows"] if row["source"]["key"] in keys]
        require(len(matches) == 7, "retained_mapping_incomplete")
        target = context["target"]
        bucket = target.list_buckets()["Buckets"][0]["Name"]
        for row in matches:
            target.put_object(
                Bucket=bucket, Key=row["target_key"], Body=b"foreign latest"
            )
            target.delete_object(Bucket=bucket, Key=row["target_key"])
        source_id = json.loads(context["config"].read_bytes())["source_container"]
        record = next(row for row in owner.data["resources"] if row["id"] == source_id)
        owner.action(record, "stop", "--time", "2")
        require(not owner.verified(record)["State"]["Running"], "source_not_offline")
        summary["stage"] = "retained_offline_reads"
        avatar_url = f"{http.endpoint}/api/v1/avatars/{self.value['avatar_token']}"
        with urlopen(avatar_url, timeout=20) as response:
            require(
                hashlib.sha256(response.read(1024 * 1024)).hexdigest()
                == self.value["avatar_sha256"],
                "avatar_http_changed",
            )
        result_path = owner.private / "retained-result.json"
        self.child(
            owner,
            context["environment"],
            (
                "from app.workers.celery_app import celery_app\nfrom verify_storage_migration_finalization import private_write\nimport json\n"
                + f"result=celery_app.send_task({self.task_name!r},queue='default').get(timeout=90)\nprivate_write({str(result_path)!r},json.dumps(result).encode())"
            ),
        )
        result = json.loads(result_path.read_bytes())
        require(
            len(result["verified"]) == 7 and result["saved_receipts_unchanged"],
            "retained_worker_failed",
        )
        with urlopen(result["media_url"], timeout=10) as response:
            require(
                response.read(1024) == b"owned-independent-media",
                "media_signature_changed",
            )
        pending_id = str(
            uuid.UUID(
                self.child(
                    owner,
                    context["environment"],
                    (
                        "from app.workers.celery_app import celery_app\n"
                        + f"print(celery_app.send_task({self.hold_task_name!r},queue='default').id)"
                    ),
                )
            )
        )
        deadline = time.monotonic() + 20
        while not Path(self.value["read_started"]).exists():
            require(time.monotonic() < deadline, "retained_get_not_started")
            time.sleep(0.05)
        pending = op(
            "withdraw-reads", expected_revision=publication["revision"], expected=2
        )
        require(
            pending.get("code") == "read_publication_unsettled",
            "retained_withdraw_did_not_wait",
        )
        current = op("status-reads")["item"]
        require(current["state"] == "withdrawing", "retained_withdrawing_not_durable")
        private_write(self.value["read_continue"], b"owned")
        self.child(
            owner,
            context["environment"],
            (
                "from app.workers.celery_app import celery_app\n"
                + f"result=celery_app.AsyncResult({pending_id!r}).get(timeout=30)\nassert result['denied_after_withdrawal'] is True"
            ),
        )
        require(
            op("withdraw-reads", expected_revision=current["revision"])["item"]["state"]
            == "withdrawn",
            "retained_not_withdrawn",
        )
        summary.update(
            stage="complete",
            source_offline=True,
            foreign_latest_and_marker=True,
            avatar_http=True,
            independent_worker_families=result["verified"],
            saved_receipts_unchanged=True,
            real_open_get_withdrawal_settled=True,
            media_exact_signed_get=True,
            migration_ready=False,
            http_completed_requests=http.requests,
        )


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--retained-reads"]))
