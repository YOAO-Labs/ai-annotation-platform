"""Qualify cache owners and bounded Range reads on an owned offline migration.

HTTP exercises saved schema-2 capabilities. Independent Worker reads use actual
thumbnail/cache readers and explicit image/video physical plans, not a complete
rendering, frame preparation or business authorization qualification.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.request import Request, urlopen

from storage_s3_contract import require
from verify_storage_migration_finalization import ROOT, operator, private_write
from verify_storage_migration_retained_reads import RetainedReads


def receipt(raw):
    return {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


async def saved_facts(db):
    from sqlalchemy import text

    result = {}
    for table in (
        "thumbnail_outputs",
        "image_pyramid_outputs",
        "video_derivative_outputs",
    ):
        result[table] = list(
            (
                await db.scalars(text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY id"))
            ).all()
        )
    result["original_cache_entries"] = list(
        (
            await db.scalars(
                text(
                    "SELECT to_jsonb(t) - ARRAY['last_accessed_at','next_check_at']::text[] FROM original_cache_entries t ORDER BY id"
                )
            )
        ).all()
    )
    result["cache_locators"] = list(
        (
            await db.scalars(
                text(
                    "SELECT to_jsonb(t) FROM dataset_asset_locations t WHERE role='original_cache' ORDER BY asset_id,location_id,bucket"
                )
            )
        ).all()
    )
    return hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()


async def seed_caches(input_path, output_path):
    from PIL import Image
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.dataset_asset import DatasetAsset, StorageLocation
    from app.db.models.dataset_source import DatasetSource
    from app.db.models.image_pyramid import ImagePyramidOutput
    from app.db.models.original_cache import OriginalCacheEntry
    from app.db.models.video_derivative import VideoDerivativeOutput
    from app.services import (
        content_snapshot,
        media_cache_storage,
        media_cache_urls,
        original_cache,
        thumbnail_outputs,
    )
    from app.services.storage import storage_service
    from app.workers._db import task_session

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    actor_id = uuid.UUID(value["actor_id"])
    instant = datetime.now(timezone.utc)
    image = io.BytesIO()
    Image.new("RGB", (64, 48), (37, 101, 163)).save(image, format="PNG")
    png = image.getvalue()
    webp = io.BytesIO()
    Image.open(io.BytesIO(png)).save(webp, format="WEBP")
    encoded = webp.getvalue()
    fixtures, all_keys = [], []
    async with task_session() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == value["database"],
            "cache_database_changed",
        )
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        thumbnail = await thumbnail_outputs.create(
            db,
            owner_kind="task_thumbnail",
            owner_id=uuid.uuid4(),
            input_ref={},
            body=encoded,
        )
        thumbnail_key = thumbnail_outputs.owner(thumbnail).key("thumbnail.webp")
        fixtures.append(
            {
                "kind": "thumbnail",
                "id": str(thumbnail.id),
                "objects": {thumbnail_key: base64.b64encode(encoded).decode()},
                "reference": thumbnail.storage_ref,
            }
        )
        for family, kind, model, legacy in (
            ("image-pyramids", "image-pyramid", ImagePyramidOutput, False),
            ("video-derivatives", "video-derivative", VideoDerivativeOutput, False),
            ("video-derivatives", "video-derivative", VideoDerivativeOutput, True),
        ):
            identifier = uuid.uuid4()
            reference = await media_cache_storage.allocate(
                db, f"{family}/objects/{identifier}/"
            )
            owner = media_cache_storage.resolve_ref(reference)
            if legacy:
                require(reference["key_prefix"] == "", "legacy_fixture_root_changed")
                reference = {
                    key: item for key, item in reference.items() if key != "key_prefix"
                }
                reference["schema"] = 1
            if kind == "image-pyramid":
                row = model(
                    id=identifier,
                    asset_id=uuid.uuid4(),
                    generation=1,
                    lease_token=uuid.uuid4(),
                    storage_ref=reference,
                    state="building",
                )
                objects = {
                    owner.key("manifest.json"): b'{"owned":"migration-cache"}',
                    owner.key("overview.webp"): encoded,
                    owner.key("tiles/0/0/0.webp"): encoded,
                }
            else:
                row = model(
                    id=identifier,
                    dataset_item_id=uuid.uuid4(),
                    kind="metadata",
                    spec={},
                    original_ref={},
                    auth_ref={},
                    storage_ref=reference,
                    state="building",
                )
                # Transport bytes are deliberately synthetic; this is not codec qualification.
                objects = {
                    owner.key("metadata.json"): b'{"owned":"migration-cache"}',
                    owner.key("playback.mp4"): bytes(range(256)) * 8,
                }
            db.add(row)
            await db.flush()
            for key, raw in objects.items():
                storage_service.client.put_object(
                    Bucket=owner.bucket,
                    Key=key,
                    Body=raw,
                    ContentType="application/json"
                    if key.endswith(".json")
                    else "image/webp"
                    if key.endswith(".webp")
                    else "video/mp4",
                )
            if kind == "video-derivative":
                row.lease_token, row.lease_expires_at = uuid.uuid4(), instant
                await db.flush()
                row.result = {}
            row.inventory = {
                "schema": 1,
                "objects": {key: receipt(raw) for key, raw in objects.items()},
            }
            row.state = "ready"
            await db.flush()
            fixtures.append(
                {
                    "kind": kind,
                    "id": str(identifier),
                    "objects": {
                        key: base64.b64encode(raw).decode()
                        for key, raw in objects.items()
                    },
                    "reference": reference,
                    "legacy": legacy,
                }
            )
        # C owns reference policy and quota, A owns the saved cache, B will own
        # physical copied reads. The internal cache hit needs no original I/O.
        dataset_id, source_id, asset_id, item_id, location_id = (
            uuid.uuid4() for _ in range(5)
        )
        db.add(
            Dataset(
                id=dataset_id,
                display_id="ds-" + dataset_id.hex[:12],
                name="Owned migrated cache",
                created_by=actor_id,
            )
        )
        db.add(
            StorageLocation(
                id=location_id,
                name="Owned cache policy scope",
                kind="filesystem",
                config={
                    "cache_policy": "on_demand",
                    "original_cache_quota_bytes": len(png) * 4,
                    "original_cache_ttl_seconds": 3600,
                },
            )
        )
        await db.flush()
        db.add(
            DatasetSource(
                id=source_id,
                dataset_id=dataset_id,
                location_id=location_id,
                name="Owned cache policy source",
                config={},
                cache_policy="on_demand",
            )
        )
        db.add(
            DatasetItem(
                id=item_id,
                dataset_id=dataset_id,
                file_name="owned.png",
                file_path="owned/original.png",
            )
        )
        await db.flush()
        db.add(
            DatasetAsset(
                id=asset_id,
                dataset_item_id=item_id,
                media_type="image",
                size_bytes=len(png),
                verification_state="verified",
                checksum_algorithm="sha256",
                checksum_value=receipt(png)["sha256"],
            )
        )
        await db.commit()

        @contextmanager
        def original():
            yield io.BytesIO(png), {"size": len(png)}

        captured = await content_snapshot.capture(original, receipt(png)["sha256"])
        try:
            require(
                await original_cache.store(factory, source_id, asset_id, captured)
                == "stored",
                "cache_store_failed",
            )
        finally:
            captured.close()
        entry = await db.scalar(
            select(OriginalCacheEntry).where(OriginalCacheEntry.asset_id == asset_id)
        )
        require(entry is not None and entry.state == "ready", "cache_owner_missing")
        for fixture in fixtures:
            fixture["urls"] = {
                key: media_cache_urls.sign(
                    fixture["kind"],
                    fixture["id"],
                    key,
                    fixture["reference"],
                    receipt(base64.b64decode(raw)),
                    3600,
                )
                for key, raw in fixture["objects"].items()
                if not fixture.get("legacy")
            }
            all_keys.extend(fixture["objects"])
        all_keys.append(original_cache.object_key(entry))
        private_write(
            output_path,
            json.dumps(
                {
                    "fixtures": fixtures,
                    "keys": all_keys,
                    "source_id": str(source_id),
                    "asset_id": str(asset_id),
                    "cache_entry_id": str(entry.id),
                    "original_sha256": receipt(png)["sha256"],
                    "saved_facts": await saved_facts(db),
                }
            ).encode(),
        )


async def worker_read(fixture_path, *, hold=False):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.db.models.thumbnail_output import ThumbnailOutput
    from app.services import (
        image_pyramid_objects as images,
        image_pyramid_storage as image_io,
    )
    from app.services import (
        video_derivative_objects as videos,
        video_derivative_storage as video_io,
    )
    from app.services import (
        media_cache_response,
        media_cache_urls,
        original_cache,
        thumbnail_outputs,
    )
    from app.services.content_snapshot import ContentError
    from app.services.prediction_inputs import settled_io
    from app.workers._db import task_session

    value = json.loads(Path(fixture_path).read_bytes())
    if hold:
        fixture = next(
            row for row in value["fixtures"] if row["kind"] == "video-derivative"
        )
        key = next(key for key in fixture["objects"] if key.endswith(".mp4"))
        from urllib.parse import parse_qs, urlsplit

        capability = media_cache_urls.decode(
            parse_qs(urlsplit(fixture["urls"][key]).query)["token"][0],
            kind=fixture["kind"],
            output_id=fixture["id"],
            relative="playback.mp4",
        )
        response = await media_cache_response.response(
            capability, range_header="bytes=5-24"
        )
        original_body = response.transfer.body

        class HeldBody:
            closed = False

            def read(self, size):
                private_write(value["read_started"], b"owned")
                deadline = time.monotonic() + 60
                while not Path(value["read_continue"]).exists():
                    require(time.monotonic() < deadline, "cache_read_hold_timeout")
                    time.sleep(0.02)
                return original_body.read(size)

            def close(self):
                original_body.close()
                self.closed = True

        held = HeldBody()
        response.transfer.body = held
        delivered = 0

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            nonlocal delivered
            delivered += len(message.get("body", b""))

        try:
            await response(
                {"type": "http", "method": "GET", "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )
        except ContentError as exc:
            require(
                exc.code == "read_publication_changed", "cache_withdraw_error_changed"
            )
            require(delivered == 0, "cache_delivered_after_withdraw")
            return {
                "denied_after_withdrawal": True,
                "physical_body_closed": held.closed
                and original_body._raw_stream.closed
                and not response.transfer.entered,
            }
        raise ValueError("cache_withdraw_did_not_reject")
    verified, urls = [], []
    async with task_session() as db:
        factory = async_sessionmaker(db.bind, expire_on_commit=False)
        require(
            await saved_facts(db) == value["saved_facts"],
            "cache_saved_facts_changed_before",
        )
        for fixture in value["fixtures"]:
            if fixture["kind"] == "thumbnail":
                row = await db.get(ThumbnailOutput, uuid.UUID(fixture["id"]))
                raw = await thumbnail_outputs.read_bytes(db, row.id)
                require(
                    raw == base64.b64decode(next(iter(fixture["objects"].values()))),
                    "thumbnail_bytes_changed",
                )
            else:
                objects, physical = (
                    (images, image_io)
                    if fixture["kind"] == "image-pyramid"
                    else (videos, video_io)
                )
                row = await db.get(objects.Output, uuid.UUID(fixture["id"]))
                plan = await objects.read_plan(db, row, list(fixture["objects"]))
                require(
                    len(plan.routes) == len(fixture["objects"]), "cache_routes_missing"
                )
                await db.commit()

                async def guard(revision):
                    current = await db.get(
                        objects.Output, row.id, populate_existing=True
                    )
                    await objects.check_reads(db, current, plan)
                    await db.commit()

                async with objects.read_access(
                    db, plan.receipt, plan, guard
                ) as storage:
                    for key in fixture["objects"]:
                        if fixture["kind"] == "image-pyramid":
                            await settled_io(
                                physical.verify_object, storage, plan.receipt, key
                            )
                        else:
                            await settled_io(
                                physical.verify_file, storage, plan.receipt, key
                            )
                        urls.append(
                            {
                                "url": await settled_io(
                                    physical.signed_url,
                                    storage,
                                    plan.receipt,
                                    key,
                                    3600,
                                ),
                                "raw": fixture["objects"][key],
                                "legacy": fixture.get("legacy", False),
                            }
                        )
                await objects.check_reads(db, row, plan)
            verified.append(fixture["kind"])
        await db.commit()
        captured = await original_cache.read(
            factory, uuid.UUID(value["source_id"]), uuid.UUID(value["asset_id"])
        )
        require(captured is not None, "migrated_original_cache_missed")
        try:
            require(
                captured.sha256 == value["original_sha256"], "cache_snapshot_changed"
            )
        finally:
            captured.close()
        require(
            await saved_facts(db) == value["saved_facts"],
            "cache_saved_facts_changed_after",
        )
        verified.append("original-cache")
        return {"verified": verified, "saved_receipts_unchanged": True, "urls": urls}


class CacheReads(RetainedReads):
    def prepare(self, owner, environment, seed, source, buckets):
        from sqlalchemy.engine import make_url

        input_path, output_path = (
            owner.private / "cache-input.json",
            owner.private / "cache-fixture.json",
        )
        private_write(
            input_path,
            json.dumps(
                {
                    "database": make_url(environment["DATABASE_URL"]).database,
                    "actor_id": seed["actor_id"],
                }
            ).encode(),
        )
        self.child(
            owner,
            environment,
            "from verify_storage_migration_cache_reads import seed_caches\n"
            + f"asyncio.run(seed_caches({str(input_path)!r},{str(output_path)!r}))",
        )
        self.value = json.loads(output_path.read_bytes())
        self.value.update(
            read_started=str(owner.private / "cache-get-started"),
            read_continue=str(owner.private / "cache-get-continue"),
        )
        self.fixture_path = owner.private / "cache-worker.json"
        private_write(self.fixture_path, json.dumps(self.value).encode())
        name = "aap_owned_caches_" + owner.data["nonce"]
        self.task_name, self.hold_task_name, self.worker_app = (
            name + ".read",
            name + ".hold",
            name + ":celery_app",
        )
        private_write(
            owner.private / (name + ".py"),
            (
                "import asyncio,sys\n"
                + f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from app.workers.celery_app import celery_app\nfrom verify_storage_migration_cache_reads import worker_read\n"
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
        require(publication["state"] == "active", "cache_not_published")
        target = context["target"]
        bucket = target.list_buckets()["Buckets"][0]["Name"]
        matches = [
            row
            for row in context["final_rows"]
            if row["source"]["key"] in self.value["keys"]
        ]
        require(len(matches) == len(self.value["keys"]), "cache_mapping_incomplete")
        for row in matches:
            target.put_object(
                Bucket=bucket, Key=row["target_key"], Body=b"foreign latest"
            )
            target.delete_object(Bucket=bucket, Key=row["target_key"])
        source_id = json.loads(context["config"].read_bytes())["source_container"]
        record = next(row for row in owner.data["resources"] if row["id"] == source_id)
        owner.action(record, "stop", "--time", "2")
        require(
            not owner.verified(record)["State"]["Running"], "cache_source_not_offline"
        )
        summary["stage"] = "offline_cache_reads"
        http_reads = 0
        for fixture in self.value["fixtures"]:
            if fixture.get("legacy"):
                continue
            for key, encoded in fixture["objects"].items():
                raw, url = (
                    base64.b64decode(encoded),
                    http.endpoint + fixture["urls"][key],
                )
                for method, interval in (
                    ("GET", None),
                    ("HEAD", None),
                    ("GET", "bytes=2-7"),
                    ("GET", "bytes=-5"),
                ):
                    with urlopen(
                        Request(
                            url,
                            method=method,
                            headers={"Range": interval} if interval else {},
                        ),
                        timeout=20,
                    ) as response:
                        observed = response.read(len(raw) + 1)
                        expected = (
                            b""
                            if method == "HEAD"
                            else raw
                            if interval is None
                            else raw[2:8]
                            if interval == "bytes=2-7"
                            else raw[-5:]
                        )
                        require(
                            observed == expected
                            and response.status == (206 if interval else 200),
                            "cache_http_bytes_changed",
                        )
                        require(
                            response.headers.get("ETag")
                            == '"sha256:' + receipt(raw)["sha256"] + '"',
                            "cache_http_identity_changed",
                        )
                    http_reads += 1
        result_path = owner.private / "cache-result.json"
        self.child(
            owner,
            context["environment"],
            "from app.workers.celery_app import celery_app\nfrom verify_storage_migration_finalization import private_write\nimport json\n"
            + f"result=celery_app.send_task({self.task_name!r},queue='default').get(timeout=90)\nprivate_write({str(result_path)!r},json.dumps(result).encode())",
        )
        result = json.loads(result_path.read_bytes())
        require(
            set(result["verified"])
            == {"thumbnail", "image-pyramid", "video-derivative", "original-cache"}
            and result["saved_receipts_unchanged"],
            "cache_worker_failed",
        )
        legacy_reads = 0
        for signed in result["urls"]:
            url = signed["url"]
            with urlopen(
                url if signed["legacy"] else http.endpoint + url, timeout=20
            ) as response:
                raw = base64.b64decode(signed["raw"])
                require(
                    response.read(len(raw) + 1) == raw, "cache_worker_signature_changed"
                )
            legacy_reads += int(signed["legacy"])
        require(legacy_reads == 2, "cache_legacy_signatures_missing")
        pending_id = str(
            uuid.UUID(
                self.child(
                    owner,
                    context["environment"],
                    "from app.workers.celery_app import celery_app\n"
                    + f"print(celery_app.send_task({self.hold_task_name!r},queue='default').id)",
                )
            )
        )
        deadline = time.monotonic() + 30
        while not Path(self.value["read_started"]).exists():
            require(time.monotonic() < deadline, "cache_stream_not_started")
            time.sleep(0.05)
        pending = op(
            "withdraw-reads", expected_revision=publication["revision"], expected=2
        )
        require(
            pending.get("code") == "read_publication_unsettled",
            "cache_withdraw_did_not_wait",
        )
        current = op("status-reads")["item"]
        require(current["state"] == "withdrawing", "cache_withdrawing_not_durable")
        private_write(self.value["read_continue"], b"owned")
        self.child(
            owner,
            context["environment"],
            "from app.workers.celery_app import celery_app\n"
            + f"result=celery_app.AsyncResult({pending_id!r}).get(timeout=30)\nassert result['denied_after_withdrawal'] and result['physical_body_closed']",
        )
        require(
            op("withdraw-reads", expected_revision=current["revision"])["item"]["state"]
            == "withdrawn",
            "cache_not_withdrawn",
        )
        summary.update(
            stage="complete",
            source_offline=True,
            foreign_latest_and_marker=True,
            cache_http_requests=http_reads,
            cache_http_families=sorted(
                {row["kind"] for row in self.value["fixtures"] if not row.get("legacy")}
            ),
            legacy_exact_signed_gets=legacy_reads,
            independent_worker_families=result["verified"],
            saved_receipts_unchanged=True,
            real_range_get_withdrawal_settled=True,
            http_completed_requests=http.requests,
            migration_ready=False,
        )


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--cache-reads"]))
