"""Exercise sealed original routes on owned S3, HTTP and an independent Worker.

This extends the finalization fixture rather than adopting a running source.
The private Worker task exists only in the fixture's generated module.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid

from storage_s3_contract import require
from verify_storage_migration_finalization import (
    ROOT,
    browser_pause,
    command,
    operator,
    private_write,
)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str).encode()
    ).hexdigest()


async def business_facts(db):
    from sqlalchemy import text

    facts = {}
    for table in (
        "tasks",
        "dataset_items",
        "dataset_assets",
        "dataset_asset_locations",
        "task_originals",
        "managed_original_creations",
    ):
        # These are closed table names and an entirely fixture-owned database.
        rows = (
            (
                await db.execute(
                    text(
                        f"SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text"
                    )
                )
            )
            .scalars()
            .all()
        )
        facts[table] = rows
    return digest(facts)


async def seed_originals(input_path, output_path):
    from sqlalchemy import select, text
    from PIL import Image
    from app.db.models.user import User
    from app.db.models.project import Project
    from app.db.models.dataset import Dataset, DatasetItem
    from app.db.models.dataset_asset import DatasetAsset, DatasetAssetLocation
    from app.db.models.task import Task
    from app.services import managed_original_creation as creation, task_originals
    from app.services.dataset_assets import LEGACY_LOCATION_ID
    from app.services.storage import storage_service
    from app.workers._db import task_session

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    data = Path(value["image_path"]).read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        require(image.size == (64, 48), "fixture_image_changed")
    sha = hashlib.sha256(data).hexdigest()
    async with task_session() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == value["database"],
            "seed_database_changed",
        )
        actor = await db.get(User, uuid.UUID(value["actor_id"]))
        require(
            actor is not None and actor.role == "super_admin", "fixture_actor_changed"
        )
        require(
            await db.scalar(select(Task.id).limit(1)) is None, "fixture_tasks_not_empty"
        )
        project = Project(
            id=uuid.uuid4(),
            display_id="P-" + uuid.uuid4().hex[:12],
            name="Owned migration original reads",
            type_label="Object detection",
            type_key="image-detection",
            data_type="image",
            owner_id=actor.id,
        )
        dataset = Dataset(
            id=uuid.uuid4(),
            display_id="D-" + uuid.uuid4().hex[:12],
            name="Owned canonical originals",
            created_by=actor.id,
        )
        db.add_all([project, dataset])
        await db.flush()
        key = "owned-originals/" + uuid.uuid4().hex + ".png"
        storage_service.client.put_object(
            Bucket=storage_service.datasets_bucket,
            Key=key,
            Body=data,
            ContentType="image/png",
        )
        item = DatasetItem(
            id=uuid.uuid4(),
            dataset_id=dataset.id,
            file_name="dataset.png",
            file_path=key,
            file_type="image",
            file_size=len(data),
            content_hash=sha,
            width=64,
            height=48,
        )
        db.add(item)
        await db.flush()
        asset = DatasetAsset(
            id=uuid.uuid4(),
            dataset_item_id=item.id,
            media_type="image",
            size_bytes=len(data),
            verification_state="verified",
            checksum_algorithm="sha256",
            checksum_value=sha,
        )
        db.add(asset)
        await db.flush()
        db.add(
            DatasetAssetLocation(
                asset_id=asset.id,
                location_id=LEGACY_LOCATION_ID,
                bucket=storage_service.datasets_bucket,
                object_key=key,
                role="managed_original",
                reference_state="present",
            )
        )
        linked = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id="T-" + uuid.uuid4().hex[:12],
            file_name="dataset.png",
            file_path=key,
            file_type="image",
            dataset_item_id=item.id,
            status="pending",
            assignee_id=actor.id,
        )
        standalone = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id="T-" + uuid.uuid4().hex[:12],
            file_name="standalone.png",
            file_path="unallocated",
            file_type="image",
            status="uploading",
            assignee_id=actor.id,
        )
        db.add_all([linked, standalone])
        await db.flush()
        allocation = await creation.allocate_task(
            db,
            project_id=project.id,
            task_id=standalone.id,
            logical_path=standalone.file_name,
            max_bytes=len(data),
            requested_by=actor.id,
            content_type="image/png",
        )
        standalone.file_path = allocation["object_key"]
        await db.commit()
        storage_service.client.put_object(
            Bucket=allocation["staging_bucket"],
            Key=allocation["staging_key"],
            Body=data,
            ContentType="image/png",
        )
        created = await creation.complete_direct(db, allocation)
        await task_originals.publish(db, standalone, allocation, created)
        standalone.status = "pending"
        await db.commit()
        result = {
            "actor_id": str(actor.id),
            "project_id": str(project.id),
            "dataset_id": str(dataset.id),
            "item_id": str(item.id),
            "asset_id": str(asset.id),
            "task_id": str(standalone.id),
            "original_id": str(standalone.original_id),
            "linked_task_id": str(linked.id),
            "sha256": sha,
            "size_bytes": len(data),
            "keys": [key, allocation["object_key"]],
            "business_facts_sha256": await business_facts(db),
        }
        private_write(output_path, json.dumps(result).encode())


async def worker_read(fixture_path, *, capture=None):
    from starlette.requests import Request
    from app.db.models.user import User
    from app.services import original_binary
    from app.workers._db import task_session

    value = json.loads(Path(fixture_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    request = Request(
        {
            "type": "http",
            "headers": [(b"authorization", ("Bearer " + value["token"]).encode())],
        }
    )
    async with task_session() as db:
        actor = await db.get(User, uuid.UUID(value["actor_id"]))
        resource = original_binary.Resource(
            kind="task", task_id=uuid.UUID(value["task_id"])
        )
        expected = dict(
            request=request,
            expected_content_id=uuid.UUID(value["original_id"]),
            expected_sha256=value["sha256"],
        )
        if capture is None:
            stage = await original_binary.snapshot(db, resource, actor, **expected)
        else:
            context = await original_binary.authorize_original(
                db, resource, actor, **expected
            )
            stage = await original_binary.capture_authorized(db, context, capture)
        try:
            require(
                hashlib.sha256(stage.file.read()).hexdigest() == value["sha256"],
                "worker_bytes_changed",
            )
            return {"sha256": stage.sha256, "size_bytes": stage.size}
        finally:
            stage.close()


async def worker_hold(fixture_path):
    from contextlib import contextmanager
    from fastapi import HTTPException
    from app.services.content_snapshot import ContentError
    from app.services.original_binary_response import capture_download

    value = json.loads(Path(fixture_path).read_bytes())

    async def capture(spec, **options):
        factory = options["client_context"]

        @contextmanager
        def paused_client():
            with factory() as client:

                def pause(http_response, parsed, **kwargs):
                    if parsed.get("Body") is None:
                        return
                    # A real successful S3 response is open while its production
                    # caller still owns the durable target Permit.
                    private_write(value["read_started"], b"owned")
                    deadline = time.monotonic() + 40
                    try:
                        while not Path(value["read_continue"]).exists():
                            require(
                                time.monotonic() < deadline, "owned_read_wait_budget"
                            )
                            time.sleep(0.05)
                    except BaseException:
                        parsed["Body"].close()
                        raise

                client.meta.events.register("after-call.s3.GetObject", pause)
                yield client

        return await capture_download(
            spec, **{**options, "client_context": paused_client}
        )

    try:
        await worker_read(fixture_path, capture=capture)
    except (ContentError, HTTPException):
        require(Path(value["read_started"]).is_file(), "physical_get_not_started")
        return {"denied_after_withdrawal": True}
    raise ValueError("withdrawn_read_delivered")


async def verify_facts(fixture_path):
    from app.workers._db import task_session

    value = json.loads(Path(fixture_path).read_bytes())
    async with task_session() as db:
        require(
            await business_facts(db) == value["business_facts_sha256"],
            "business_facts_changed",
        )


class OriginalReads:
    def prepare(self, owner, environment, seed, source, buckets):
        from PIL import Image

        image = io.BytesIO()
        Image.new("RGB", (64, 48), (31, 97, 151)).save(image, format="PNG")
        self.raw = image.getvalue()
        image_path = owner.private / "original.png"
        private_write(image_path, self.raw)
        input_path, output_path = (
            owner.private / "original-input.json",
            owner.private / "original-fixture.json",
        )
        from sqlalchemy.engine import make_url

        private_write(
            input_path,
            json.dumps(
                {
                    "database": make_url(environment["DATABASE_URL"]).database,
                    "actor_id": seed["actor_id"],
                    "image_path": str(image_path),
                }
            ).encode(),
        )
        self.child(
            owner,
            environment,
            f"asyncio.run(seed_originals({str(input_path)!r},{str(output_path)!r}))",
        )
        self.fixture_path = output_path
        self.worker_path = owner.private / "worker-original.json"
        self.value = json.loads(output_path.read_bytes())
        name = "aap_owned_reads_" + owner.data["nonce"]
        self.task_name = name + ".read"
        self.hold_task_name = name + ".hold"
        self.worker_app = name + ":celery_app"
        private_write(
            owner.private / (name + ".py"),
            (
                "import asyncio,sys\n"
                f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from app.workers.celery_app import celery_app\n"
                "from verify_storage_migration_original_reads import worker_read,worker_hold\n"
                f"@celery_app.task(name={self.task_name!r})\n"
                f"def read(): return asyncio.run(worker_read({str(self.worker_path)!r}))\n"
                f"@celery_app.task(name={self.hold_task_name!r})\n"
                f"def hold(): return asyncio.run(worker_hold({str(self.worker_path)!r}))\n"
            ).encode(),
        )
        environment["PYTHONPATH"] = (
            str(owner.private) + os.pathsep + str(ROOT / "apps/api")
        )

    def child(self, owner, environment, invocation):
        path = owner.private / ("read-check-" + uuid.uuid4().hex + ".py")
        private_write(
            path,
            (
                "import asyncio,sys\n"
                f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from verify_storage_migration_original_reads import seed_originals,verify_facts\n"
                + invocation
                + "\n"
            ).encode(),
        )
        return command([sys.executable, str(path)], env=environment)

    def after_seal(self, **context):
        owner, http, summary = (context[key] for key in ("owner", "http", "summary"))
        native_environment = context["native_environment"]

        def op(action, **flags):
            return operator(
                owner,
                native_environment,
                action,
                context["seed"]["actor_id"],
                context["finalization_id"],
                context["config"],
                context["state"],
                **flags,
            )

        summary["stage"] = "publish_reads"
        publication = op(
            "publish-reads", expected_revision=context["sealed"]["revision"]
        )["item"]
        require(publication["state"] == "active", "reads_not_published")
        require(op("status-reads")["item"] == publication, "publication_status_changed")
        denied = op(
            "invalidate", expected_revision=context["sealed"]["revision"], expected=2
        )
        require(
            denied.get("code") == "read_publication_active",
            "publication_fence_not_retained",
        )
        self.value["token"] = http.token
        self.value["read_started"] = str(owner.private / "physical-get-started")
        self.value["read_continue"] = str(owner.private / "physical-get-continue")
        private_write(self.worker_path, json.dumps(self.value).encode())
        matches = [
            row
            for row in context["final_rows"]
            if row["source"]["key"] in self.value["keys"]
        ]
        require(
            len(matches) == 2 and all(row["state"] == "verified" for row in matches),
            "original_routes_not_verified",
        )
        target = context["target"]
        for row in matches:
            bucket = (
                context["sealed"]["target_identity"]["bucket"]
                if "target_identity" in context["sealed"]
                else None
            )
            if bucket is None:
                # Copy target is one owned bucket; obtain it from the fixture's
                # own client, never from another runtime or an arbitrary input.
                owned = target.list_buckets()["Buckets"]
                require(len(owned) == 1, "owned_target_bucket_changed")
                bucket = owned[0]["Name"]
            target.put_object(
                Bucket=bucket, Key=row["target_key"], Body=b"foreign latest"
            )
            target.delete_object(Bucket=bucket, Key=row["target_key"])
        source_id = json.loads(context["config"].read_bytes())["source_container"]
        source_record = next(
            item for item in owner.data["resources"] if item["id"] == source_id
        )
        owner.action(source_record, "stop", "--time", "2")
        require(
            not owner.verified(source_record)["State"]["Running"], "source_not_offline"
        )
        summary["stage"] = "offline_original_reads"
        for task in (self.value["task_id"], self.value["linked_task_id"]):
            path = "/api/v1/files/tasks/" + task
            identity = http.call("GET", path + "/original-identity")
            raw = http.call("GET", identity["download_path"], binary=True)
            require(raw == self.raw, "http_original_bytes_changed")
            raw = http.call(
                "GET", path + "/content?sha256=" + self.value["sha256"], binary=True
            )
            require(raw == self.raw, "http_image_bytes_changed")
        item_path = (
            "/api/v1/datasets/"
            + self.value["dataset_id"]
            + "/items/"
            + self.value["item_id"]
        )
        identity = http.call("GET", item_path + "/original-identity")
        require(
            http.call("GET", identity["download_path"], binary=True) == self.raw,
            "http_item_bytes_changed",
        )
        # The production HTTP boundary must still require a current credential.
        token, http.token = http.token, None
        try:
            http.call("GET", identity["download_path"], expected=401)
        finally:
            http.token = token
        child = (
            "from app.workers.celery_app import celery_app\n"
            f"result=celery_app.send_task({self.task_name!r},queue='default').get(timeout=60)\n"
            f"assert result['sha256']=={self.value['sha256']!r}\n"
        )
        self.child(owner, context["environment"], child)
        self.child(
            owner,
            context["environment"],
            f"asyncio.run(verify_facts({str(self.fixture_path)!r}))",
        )
        if context["args"].pause_for_browser:
            private_write(
                owner.private / "original-browser.json", json.dumps(self.value).encode()
            )
            summary["stage"] = "browser_pause"
            browser_pause(
                owner,
                http,
                context["api_port"],
                context["endpoint"],
                context["seed"]["actor_id"],
                context["window_id"],
                context["child"],
                context["first"],
                context["email"],
                context["password"],
                context["args"].browser_wait_seconds,
            )
        summary["stage"] = "withdraw_reads"
        pending_id = self.child(
            owner,
            context["environment"],
            (
                "from app.workers.celery_app import celery_app\n"
                f"print(celery_app.send_task({self.hold_task_name!r},queue='default').id)\n"
            ),
        )
        pending_id = str(uuid.UUID(pending_id))
        deadline = time.monotonic() + 20
        while not Path(self.value["read_started"]).exists():
            require(time.monotonic() < deadline, "physical_get_not_started")
            time.sleep(0.05)
        pending = op(
            "withdraw-reads", expected_revision=publication["revision"], expected=2
        )
        require(
            pending.get("code") == "read_publication_unsettled",
            "withdrawal_did_not_wait",
        )
        current = op("status-reads")["item"]
        require(current["state"] == "withdrawing", "withdrawing_not_durable")
        http.call("GET", identity["download_path"], expected=409)
        private_write(self.value["read_continue"], b"owned")
        self.child(
            owner,
            context["environment"],
            (
                "from app.workers.celery_app import celery_app\n"
                f"result=celery_app.AsyncResult({pending_id!r}).get(timeout=30)\n"
                "assert result['denied_after_withdrawal'] is True\n"
            ),
        )
        withdrawn = op("withdraw-reads", expected_revision=current["revision"])["item"]
        require(withdrawn["state"] == "withdrawn", "publication_not_withdrawn")
        http.call("GET", identity["download_path"], expected=409)
        from storage_source_fence_state import Journal
        from storage_source_fence import descriptor

        _, config_digest = descriptor(context["config"])
        with Journal(context["state"], config_digest) as journal:
            require(
                journal.data.get("application_claim", {}).get("finalization_id")
                == context["finalization_id"],
                "withdrawal_released_fence",
            )
        summary.update(
            stage="complete",
            original_count=2,
            managed_read_http_count=5,
            independent_worker_original=True,
            source_offline=True,
            foreign_latest_and_marker=True,
            business_facts_unchanged=True,
            current_authority_required=True,
            withdrawn_read_denied=True,
            native_claim_retained=True,
            real_open_get_withdrawal_settled=True,
            migration_ready=False,
            http_completed_requests=http.requests,
        )


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--original-reads"]))
