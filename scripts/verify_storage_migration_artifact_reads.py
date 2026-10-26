"""Qualify saved training/ZIP reads on an owned, sealed migration fixture."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.request import urlopen
import zipfile

from storage_s3_contract import require
from verify_storage_migration_finalization import ROOT, command, operator, private_write
from verify_storage_migration_original_reads import digest


async def facts(db):
    from sqlalchemy import text

    tables = {}
    for table in ("training_dataset_versions", "export_outputs"):
        tables[table] = (
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
    return digest(tables)


async def seed_artifacts(input_path, output_path):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select, text
    from training_contract.dataset import instance_rle_evidence
    from app.db.models.project import Project
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.export_output import ExportOutput
    from app.db.models.user import User
    from app.services import export_storage, training_dataset_storage as owners
    from app.services.training_dataset_bridge import content_digest
    from app.services.storage import storage_service
    from app.workers._db import task_session

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    raw = Path(value["image_path"]).read_bytes()
    package = Path(value["package_path"]).read_bytes()
    async with task_session() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == value["database"],
            "seed_database_changed",
        )
        actor = await db.get(User, uuid.UUID(value["actor_id"]))
        require(actor is not None and actor.role == "super_admin", "actor_changed")
        require(
            await db.scalar(select(TrainingDatasetVersion.id).limit(1)) is None,
            "fixture_training_not_empty",
        )
        project = Project(
            id=uuid.uuid4(),
            display_id="P-" + uuid.uuid4().hex[:12],
            name="Owned migrated artifacts",
            type_label="Object detection",
            type_key="image-detection",
            data_type="image",
            owner_id=actor.id,
        )
        db.add(project)
        await db.flush()
        task_id, annotation_id = uuid.uuid4(), uuid.uuid4()
        rle = {"encoding": "coco_rle", "size": [48, 64], "counts": [0, 1, 3071]}
        masks = json.dumps(
            {
                "instances": [
                    {
                        "annotation_id": str(annotation_id),
                        "annotation_version": 1,
                        "rle": rle,
                        **instance_rle_evidence(rle),
                    }
                ]
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        sample = {
            "task_id": str(task_id),
            "included": True,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size_bytes": len(raw),
            "label_sha256": hashlib.sha256(masks).hexdigest(),
            "width": 64,
            "height": 48,
        }
        snapshot = {"schema": 1, "samples": [sample], "classes": ["shape"]}
        version = TrainingDatasetVersion(
            id=uuid.uuid4(),
            project_id=project.id,
            creator_id=actor.id,
            request_id=uuid.uuid4(),
            request_digest=content_digest(snapshot),
            name="Owned frozen package",
            status="pending",
            snapshot=snapshot,
            snapshot_digest=content_digest(snapshot),
            manifest={"schema": 1, "samples": [sample]},
        )
        db.add(version)
        await db.flush()
        await owners.allocate(db, version)
        owner = owners.resolve(version)
        version.package_key = owner.key("dataset.zip")
        version.package_sha256 = hashlib.sha256(package).hexdigest()
        version.size_bytes = len(package)
        version.status = "ready"
        keys = [
            version.package_key,
            owner.key(f"media/{task_id}"),
            owner.key(f"masks/{task_id}.json"),
        ]
        for key, data, kind in zip(
            keys,
            (package, raw, masks),
            ("application/zip", "image/png", "application/json"),
            strict=True,
        ):
            storage_service.client.put_object(
                Bucket=owner.bucket,
                Key=key,
                Body=data,
                ContentType=kind,
                Metadata={
                    "snapshot-digest": version.snapshot_digest,
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            )
        created = datetime.now(timezone.utc)
        output = ExportOutput(
            id=uuid.uuid4(),
            project_id=project.id,
            job_id=uuid.uuid4(),
            media="image",
            format="owned-fixture",
            file_count=1,
            sha256=hashlib.sha256(package).hexdigest(),
            size_bytes=len(package),
            state="pending",
            created_at=created,
            expires_at=created + timedelta(days=7),
        )
        output.storage_ref = await export_storage.allocate(db, output)
        db.add(output)
        await db.flush()
        destination = export_storage.resolve(output)
        storage_service.client.put_object(
            Bucket=destination.bucket,
            Key=destination.object_key,
            Body=package,
            ContentType="application/zip",
        )
        output.state = "ready"
        await db.commit()
        private_write(
            output_path,
            json.dumps(
                {
                    "actor_id": str(actor.id),
                    "project_id": str(project.id),
                    "version_id": str(version.id),
                    "output_id": str(output.id),
                    "annotation_id": str(annotation_id),
                    "sample": sample,
                    "keys": [*keys, destination.object_key],
                    "package_sha256": output.sha256,
                    "business_facts_sha256": await facts(db),
                }
            ).encode(),
        )


async def worker_read(fixture_path, *, hold=False):
    from types import SimpleNamespace
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.export_output import ExportOutput
    from app.services import (
        export_outputs,
        export_storage,
        storage_migration_artifact_reads as reads,
        training_dataset_analysis as analysis,
        training_dataset_storage as owners,
        training_storage_io as io,
    )
    from app.services.content_snapshot import ContentError
    from app.services.training_dataset_bridge import TrainingDatasetError
    from app.workers._db import task_session

    value = json.loads(Path(fixture_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    async with task_session() as db:
        version = await db.get(TrainingDatasetVersion, uuid.UUID(value["version_id"]))
        owner = owners.resolve(version)
        sample = SimpleNamespace(**value["sample"])
        sample.task_id = uuid.UUID(sample.task_id)

        def media(bound):
            if hold:

                def pause(http_response, parsed, **kwargs):
                    if parsed.get("Body") is None:
                        return
                    private_write(value["read_started"], b"owned")
                    deadline = time.monotonic() + 40
                    try:
                        while not Path(value["read_continue"]).exists():
                            require(time.monotonic() < deadline, "read_wait_budget")
                            time.sleep(0.05)
                    except BaseException:
                        parsed["Body"].close()
                        raise

                bound.client.meta.events.register("after-call.s3.GetObject", pause)
            return analysis.read_media(
                version.project_id, version, sample, transport=bound
            )

        try:
            staged, kind = await io.read(
                db,
                owner,
                "training_dataset_media",
                media,
                object_key=owner.key(f"media/{sample.task_id}"),
                sha256=sample.sha256,
                size_bytes=sample.size_bytes,
                close_result=lambda result: result[0].close(),
            )
        except (ContentError, TrainingDatasetError):
            require(hold and Path(value["read_started"]).is_file(), "unexpected_denial")
            return {"denied_after_withdrawal": True}
        try:
            require(kind == "image/png", "frozen_media_type_changed")
            require(
                hashlib.sha256(staged.read()).hexdigest() == sample.sha256,
                "media_changed",
            )
        finally:
            staged.close()
        require(not hold, "withdrawn_read_delivered")
        mask = await io.read(
            db,
            owner,
            "training_dataset_mask",
            lambda bound: analysis.read_instance_mask(
                version.project_id,
                version,
                sample,
                value["annotation_id"],
                transport=bound,
            ),
            object_key=owner.key(f"masks/{sample.task_id}.json"),
            sha256=sample.label_sha256,
        )
        require(mask["annotation_id"] == value["annotation_id"], "frozen_mask_changed")
        output = await db.get(ExportOutput, uuid.UUID(value["output_id"]))
        receipt = export_outputs.snapshot(output)
        route = await reads.resolve(
            db,
            receipt.storage_ref,
            export_outputs.object_key(receipt),
            receipt.sha256,
            receipt.size_bytes,
        )
        require(route is not None, "zip_route_missing")
        require(
            await reads.run(
                db,
                route,
                export_outputs.read_purpose(receipt.id),
                lambda client: export_storage.verify_read(client, receipt),
            ),
            "zip_not_verified",
        )
        prepared = await export_outputs.prepare_download(
            db, receipt, download_name="owned.zip"
        )
        url = await export_outputs.prepared_download(
            db, receipt, download_name="owned.zip", prepared=prepared
        )
        require(
            await facts(db) == value["business_facts_sha256"], "artifact_facts_changed"
        )
        return {
            "zip_url": url,
            "media_and_mask_verified": True,
            "business_facts_unchanged": True,
        }


class ArtifactReads:
    def child(self, owner, environment, invocation):
        path = owner.private / ("artifact-check-" + uuid.uuid4().hex + ".py")
        private_write(
            path,
            (
                "import asyncio,sys\n"
                f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from verify_storage_migration_artifact_reads import seed_artifacts\n"
                + invocation
                + "\n"
            ).encode(),
        )
        return command([sys.executable, str(path)], env=environment)

    def prepare(self, owner, environment, seed, source, buckets):
        from PIL import Image
        from sqlalchemy.engine import make_url

        raw = io.BytesIO()
        Image.new("RGB", (64, 48), (31, 97, 151)).save(raw, format="PNG")
        image_path, package_path = (
            owner.private / "frozen.png",
            owner.private / "frozen.zip",
        )
        private_write(image_path, raw.getvalue())
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "frozen.txt", "Owned saved artifact, unchanged after migration"
            )
        self.package = package.getvalue()
        private_write(package_path, self.package)
        input_path, self.fixture_path = (
            owner.private / "artifact-input.json",
            owner.private / "artifact-fixture.json",
        )
        private_write(
            input_path,
            json.dumps(
                {
                    "database": make_url(environment["DATABASE_URL"]).database,
                    "actor_id": seed["actor_id"],
                    "image_path": str(image_path),
                    "package_path": str(package_path),
                }
            ).encode(),
        )
        self.child(
            owner,
            environment,
            f"asyncio.run(seed_artifacts({str(input_path)!r},{str(self.fixture_path)!r}))",
        )
        self.value = json.loads(self.fixture_path.read_bytes())
        self.fixture_path = owner.private / "artifact-worker.json"
        self.value.update(
            read_started=str(owner.private / "artifact-get-started"),
            read_continue=str(owner.private / "artifact-get-continue"),
        )
        private_write(self.fixture_path, json.dumps(self.value).encode())
        name = "aap_owned_artifacts_" + owner.data["nonce"]
        self.task_name, self.hold_task_name = name + ".read", name + ".hold"
        self.worker_app = name + ":celery_app"
        private_write(
            owner.private / (name + ".py"),
            (
                "import asyncio,sys\n"
                f"sys.path.insert(0,{str(ROOT / 'scripts')!r})\n"
                "from app.workers.celery_app import celery_app\n"
                "from verify_storage_migration_artifact_reads import worker_read\n"
                f"@celery_app.task(name={self.task_name!r})\n"
                f"def read(): return asyncio.run(worker_read({str(self.fixture_path)!r}))\n"
                f"@celery_app.task(name={self.hold_task_name!r})\n"
                f"def hold(): return asyncio.run(worker_read({str(self.fixture_path)!r},hold=True))\n"
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
        require(publication["state"] == "active", "reads_not_published")
        target = context["target"]
        owned = target.list_buckets()["Buckets"]
        require(len(owned) == 1, "target_bucket_changed")
        matches = [
            row
            for row in context["final_rows"]
            if row["source"]["key"] in self.value["keys"]
        ]
        require(
            len(matches) == 4 and all(row["state"] == "verified" for row in matches),
            "artifact_routes_missing",
        )
        for row in matches:
            target.put_object(
                Bucket=owned[0]["Name"], Key=row["target_key"], Body=b"foreign latest"
            )
            target.delete_object(Bucket=owned[0]["Name"], Key=row["target_key"])
        source_id = json.loads(context["config"].read_bytes())["source_container"]
        record = next(row for row in owner.data["resources"] if row["id"] == source_id)
        owner.action(record, "stop", "--time", "2")
        require(not owner.verified(record)["State"]["Running"], "source_not_offline")
        summary["stage"] = "offline_artifact_reads"
        path = f"/api/v1/projects/{self.value['project_id']}/training-datasets/{self.value['version_id']}/download"
        signed = http.call("GET", path)
        require(
            signed["sha256"] == self.value["package_sha256"], "package_identity_changed"
        )
        with urlopen(signed["url"], timeout=10) as response:
            require(
                response.read(len(self.package) + 1) == self.package,
                "package_bytes_changed",
            )
        token, http.token = http.token, None
        try:
            http.call("GET", path, expected=401)
        finally:
            http.token = token
        result_path = owner.private / "artifact-worker-result.json"
        self.child(
            owner,
            context["environment"],
            (
                "from app.workers.celery_app import celery_app\n"
                "from verify_storage_migration_finalization import private_write\n"
                "import json\n"
                f"result=celery_app.send_task({self.task_name!r},queue='default').get(timeout=60)\n"
                f"private_write({str(result_path)!r},json.dumps(result).encode())\n"
            ),
        )
        result = json.loads(result_path.read_bytes())
        require(
            result["media_and_mask_verified"] and result["business_facts_unchanged"],
            "worker_artifacts_changed",
        )
        with urlopen(result["zip_url"], timeout=10) as response:
            require(
                response.read(len(self.package) + 1) == self.package,
                "zip_bytes_changed",
            )
        summary["stage"] = "withdraw_artifact_reads"
        pending_id = str(
            uuid.UUID(
                self.child(
                    owner,
                    context["environment"],
                    (
                        "from app.workers.celery_app import celery_app\n"
                        f"print(celery_app.send_task({self.hold_task_name!r},queue='default').id)\n"
                    ),
                )
            )
        )
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
        http.call("GET", path, expected=409)
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
        http.call("GET", path, expected=409)
        # Already-issued S3 signatures keep their ordinary expiry contract.
        with urlopen(signed["url"], timeout=10) as response:
            require(
                response.read(len(self.package) + 1) == self.package,
                "issued_signature_changed",
            )
        summary.update(
            stage="complete",
            source_offline=True,
            foreign_latest_and_marker=True,
            training_package_http=True,
            independent_worker_media_and_mask=True,
            zip_full_verify_and_signed_get=True,
            business_facts_unchanged=True,
            current_authority_required=True,
            real_open_get_withdrawal_settled=True,
            issued_signature_retains_expiry=True,
            migration_ready=False,
            http_completed_requests=http.requests,
        )


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--artifact-reads"]))
