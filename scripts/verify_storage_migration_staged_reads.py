"""Exercise saved Mask staging and terminal log reads with source A offline.

The Mask fixture runs a real API preflight and an independent Celery materialize
of a saved batch. It does not execute or qualify a complete annotation import.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from storage_s3_contract import require
from verify_storage_migration_finalization import (
    ROOT,
    browser_pause,
    operator,
    private_write,
)
from verify_storage_migration_retained_reads import RetainedReads
from verify_storage_source_fence import object_facts


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


async def saved_facts(db, value):
    """Compare exact fixture owners, including original saved plan/artifacts."""
    from sqlalchemy import text

    result = {}
    for table, name in (
        ("mask_format_uploads", "upload_id"),
        ("mask_format_imports", "import_id"),
        ("training_dataset_versions", "dataset_id"),
        ("training_runs", "run_id"),
        ("training_attempts", "attempt_id"),
        ("tasks", "task_id"),
    ):
        row = await db.scalar(
            text(f"SELECT to_jsonb(t) FROM {table} t WHERE id=:id"),
            {"id": uuid.UUID(value[name])},
        )
        require(row is not None, "staged_owner_missing")
        result[table] = row
    return sha256(json.dumps(result, sort_keys=True).encode())


async def seed_staged(input_path, output_path):
    from sqlalchemy import text
    from training_contract.models import ULTRALYTICS_VERSION
    from app.db.models.mask_format_upload import MaskFormatUpload
    from app.db.models.project import Project
    from app.db.models.task import Task
    from app.db.models.training_dataset import TrainingDatasetVersion
    from app.db.models.training_run import TrainingAttempt, TrainingRun
    from app.services import import_storage, training_run_storage
    from app.services.mask_formats import service
    from app.services.mask_formats.contracts import canonical_digest
    from app.services.storage import storage_service
    from app.workers._db import task_session

    value = json.loads(Path(input_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    actor_id, instant = uuid.UUID(value["actor_id"]), datetime.now(timezone.utc)
    async with task_session() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == value["database"],
            "staged_database_changed",
        )
        project = Project(
            id=uuid.uuid4(),
            display_id="P-STAGED-" + uuid.uuid4().hex[:8],
            name="Owned staged migration reads",
            type_key="image-det",
            type_label="图像检测",
            data_type="image",
            owner_id=actor_id,
            tool_bindings={
                "bbox": {
                    "enabled": True,
                    "classes": [{"name": "object"}],
                    "attribute_schema": {"fields": []},
                }
            },
        )
        other = Project(
            id=uuid.uuid4(),
            display_id="P-OTHER-" + uuid.uuid4().hex[:8],
            name="Owned foreign project boundary",
            type_key="image-det",
            type_label="图像检测",
            data_type="image",
            owner_id=actor_id,
        )
        db.add_all([project, other])
        await db.flush()
        task = Task(
            id=uuid.uuid4(),
            project_id=project.id,
            display_id="T-STAGED-" + uuid.uuid4().hex[:8],
            file_name="image.png",
            file_path="dataset/image.png",
            file_type="image",
            status="pending",
        )
        db.add(task)
        await db.flush()
        raw = json.dumps(
            {
                "schema_version": "1.3",
                "tasks": [
                    {
                        "task_match": {"display_id": task.display_id},
                        "file_path": task.file_path,
                        "media_type": "image",
                        "annotations": [
                            {
                                "geometry": {
                                    "type": "bbox",
                                    "x": 0.1,
                                    "y": 0.1,
                                    "w": 0.3,
                                    "h": 0.2,
                                },
                                "class_name": "object",
                                "tool_unit_id": "bbox",
                                "source": "manual",
                            }
                        ],
                    }
                ],
            },
            sort_keys=True,
        ).encode()
        upload_id = uuid.uuid4()
        prefix = f"mask-formats/{project.id}/{actor_id}/{upload_id}/"
        key = prefix + "owned.json"
        reference = await import_storage.allocate(db, prefix)
        upload = MaskFormatUpload(
            id=upload_id,
            project_id=project.id,
            uploader_id=actor_id,
            logical_object_key=key,
            content_type="application/json",
            storage_ref=reference,
            created_at=instant,
            upload_expires_at=instant + timedelta(seconds=900),
        )
        db.add(upload)
        await db.commit()
        staged_owner = await service.staging_owner(db, key, reference)
        physical_key = staged_owner.key("owned.json")
        storage_service.client.put_object(
            Bucket=staged_owner.bucket,
            Key=physical_key,
            Body=raw,
            ContentType="application/json",
        )
        preflight = await service.preflight_import(
            db,
            project=project,
            user_id=actor_id,
            format_id="aap_json",
            staged_object_key=key,
            staged_sha256=sha256(raw),
            mapping={},
            options={"overwrite": False},
        )
        # A failed dataset is only the durable FK parent of the terminal run.
        # No training preparation, execution or model qualification is claimed.
        snapshot = {"schema": 1, "samples": [], "classes": ["object"]}
        dataset = TrainingDatasetVersion(
            id=uuid.uuid4(),
            project_id=project.id,
            creator_id=actor_id,
            request_id=uuid.uuid4(),
            request_digest=canonical_digest(snapshot),
            name="Owned terminal log parent",
            status="failed",
            snapshot=snapshot,
            snapshot_digest=canonical_digest(snapshot),
        )
        db.add(dataset)
        await db.flush()
        config = {
            "resource_id": "owned-staged-log-fixture/offline",
            "task": "image-detection",
            "framework": "ultralytics",
            "framework_version": ULTRALYTICS_VERSION,
            "base_model": {"id": "yolo11n"},
            "parameters": {"epochs": 1},
        }
        run = TrainingRun(
            id=uuid.uuid4(),
            project_id=project.id,
            creator_id=actor_id,
            dataset_id=dataset.id,
            request_id=uuid.uuid4(),
            request_digest=canonical_digest({"owned": "terminal-log"}),
            name="Owned terminal migrated log",
            status="failed",
            config=config,
            config_digest=canonical_digest(config),
            error_message="合成终态日志读取验收，未执行训练",
            completed_at=instant,
        )
        db.add(run)
        await db.flush()
        await training_run_storage.allocate(db, run)
        attempt = TrainingAttempt(
            id=uuid.uuid4(),
            run_id=run.id,
            ordinal=1,
            status="failed",
            completed_at=instant,
        )
        db.add(attempt)
        await db.flush()
        name = "train.log"
        log = (
            b"owned safe log line\n"
            b'password="owned-private-password" '
            b"https://storage.example.test/file?X-Amz-Signature=owned-private-signature "
            b"/host/owned-private-path\n"
        )
        run_owner = training_run_storage.resolve(run)
        log_key = run_owner.key(f"attempts/{attempt.id}/{name}")
        artifact = {
            "role": "log",
            "key": log_key,
            "sha256": sha256(log),
            "size_bytes": len(log),
        }
        attempt.artifacts = {name: artifact}
        run.current_attempt_id = attempt.id
        storage_service.client.put_object(
            Bucket=run_owner.bucket,
            Key=log_key,
            Body=log,
            ContentType="text/plain; charset=utf-8",
            Metadata={"attempt-id": str(attempt.id), "sha256": artifact["sha256"]},
        )
        await db.commit()
        result = {
            "database": value["database"],
            "actor_id": str(actor_id),
            "project_id": str(project.id),
            "other_project_id": str(other.id),
            "upload_id": str(upload_id),
            "import_id": str(preflight.import_id),
            "task_id": str(task.id),
            "dataset_id": str(dataset.id),
            "run_id": str(run.id),
            "attempt_id": str(attempt.id),
            "log_name": name,
            "run_config": config,
            "staged_key": key,
            "staged_sha256": sha256(raw),
            "staged_size_bytes": len(raw),
            "plan": preflight.plan.model_dump(mode="json"),
            "artifact": artifact,
            "keys": [physical_key, log_key],
        }
        result["business_facts_sha256"] = await saved_facts(db, result)
        private_write(output_path, json.dumps(result).encode())


@contextmanager
def observe_staging(service, value, observed, *, hold):
    """Observe the real SDK seam; do not replace the body or transport bytes."""
    original = service.read_staged_from_client

    def read(client, owner, object_key, expected_sha256, local_path):
        physical = client._client
        original_close = physical.close
        observed["temp_directory"] = Path(local_path).parent

        def close():
            original_close()
            observed["sdk_closed"] = True

        def received(http_response, parsed, **kwargs):
            body = parsed.get("Body")
            if body is None:
                return
            require("body" not in observed, "staging_get_repeated")
            observed["body"] = body
            observed["version_id"] = parsed.get("VersionId")
            if hold:
                private_write(value["read_started"], b"owned")
                deadline = time.monotonic() + 60
                try:
                    while not Path(value["read_continue"]).exists():
                        require(time.monotonic() < deadline, "staging_wait_budget")
                        time.sleep(0.05)
                except BaseException:
                    body.close()
                    raise

        physical.close = close
        physical.meta.events.register("after-call.s3.GetObject", received)
        return original(client, owner, object_key, expected_sha256, local_path)

    service.read_staged_from_client = read
    try:
        yield
    finally:
        service.read_staged_from_client = original


async def worker_read(fixture_path, *, hold=False):
    from sqlalchemy import text
    from app.db.models.mask_format_import import MaskFormatImport
    from app.services.mask_formats import service
    from app.workers._db import task_session

    value = json.loads(Path(fixture_path).read_bytes())
    require(os.environ.get("AAP_WORKTREE_MODE") == "test", "test_mode_required")
    observed, denied = {}, False
    async with task_session() as db:
        require(
            await db.scalar(text("SELECT current_database()")) == value["database"],
            "staged_worker_database_changed",
        )
        batch = await db.get(MaskFormatImport, uuid.UUID(value["import_id"]))
        require(batch is not None, "staged_batch_missing")
        require(
            await saved_facts(db, value) == value["business_facts_sha256"],
            "staged_facts_changed_before_read",
        )
        with observe_staging(service, value, observed, hold=hold):
            try:
                async with service.materialize_staged_object(
                    db,
                    object_key=batch.staged_object_key,
                    expected_sha256=batch.staged_sha256,
                    staging_ref=batch.staging_ref,
                ) as staged:
                    require(not hold, "withdrawn_staging_delivered")
                    raw = Path(staged.local_path).read_bytes()
                    require(
                        len(raw) == value["staged_size_bytes"]
                        and sha256(raw) == value["staged_sha256"]
                        and staged.sha256 == batch.staged_sha256,
                        "materialized_staging_changed",
                    )
            except service.MaskFormatError as exc:
                require(
                    hold
                    and Path(value["read_started"]).is_file()
                    and exc.reason == "read_publication_changed",
                    "unexpected_staging_denial",
                )
                denied = True
        require(denied is hold, "staging_delivery_contract_changed")
        require(
            observed.get("body") is not None
            and observed["body"]._raw_stream.closed
            and observed.get("sdk_closed") is True,
            "staging_transport_not_closed",
        )
        require(
            not observed["temp_directory"].exists(), "staging_temp_directory_retained"
        )
        require(
            await saved_facts(db, value) == value["business_facts_sha256"],
            "staged_owner_or_plan_changed",
        )
        return {
            "materialized": not hold,
            "denied_after_withdrawal": denied,
            "body_and_sdk_closed": True,
            "temporary_directory_removed": True,
            "business_facts_unchanged": True,
            "version_id": observed["version_id"],
        }


class StagedReads(RetainedReads):
    def prepare(self, owner, environment, seed, source, buckets):
        from sqlalchemy.engine import make_url

        input_path, output_path = (
            owner.private / "staged-input.json",
            owner.private / "staged-seed.json",
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
            "from verify_storage_migration_staged_reads import seed_staged\n"
            + f"asyncio.run(seed_staged({str(input_path)!r},{str(output_path)!r}))",
        )
        self.value = json.loads(output_path.read_bytes())
        self.value.update(
            read_started=str(owner.private / "staging-get-started"),
            read_continue=str(owner.private / "staging-get-continue"),
        )
        self.fixture_path = owner.private / "staged-worker.json"
        private_write(self.fixture_path, json.dumps(self.value).encode())
        name = "aap_owned_staged_" + owner.data["nonce"]
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
                "from app.workers.celery_app import celery_app\n"
                "from verify_storage_migration_staged_reads import worker_read\n"
                + f"@celery_app.task(name={self.task_name!r})\n"
                + f"def read(): return asyncio.run(worker_read({str(self.fixture_path)!r}))\n"
                + f"@celery_app.task(name={self.hold_task_name!r})\n"
                + f"def hold(): return asyncio.run(worker_read({str(self.fixture_path)!r},hold=True))\n"
            ).encode(),
        )
        environment["PYTHONPATH"] = (
            str(owner.private) + os.pathsep + str(ROOT / "apps/api")
        )

    def result(self, context, *, task_id=None):
        result_path = context["owner"].private / ("staged-result-" + uuid.uuid4().hex)
        expression = (
            f"celery_app.AsyncResult({task_id!r}).get(timeout=30)"
            if task_id is not None
            else f"celery_app.send_task({self.task_name!r},queue='default').get(timeout=60)"
        )
        self.child(
            context["owner"],
            context["environment"],
            "from app.workers.celery_app import celery_app\n"
            "from verify_storage_migration_finalization import private_write\n"
            "import json\n"
            + f"result={expression}\n"
            + f"private_write({str(result_path)!r},json.dumps(result).encode())\n",
        )
        return json.loads(result_path.read_bytes())

    def content(self, http, path, *, expected=200):
        require(
            isinstance(path, str) and path.startswith("/projects/"),
            "log_descriptor_url_changed",
        )
        headers = {"Authorization": "Bearer " + http.token} if http.token else {}
        request = Request(http.endpoint + "/api/v1" + path, headers=headers)
        try:
            response = urlopen(request, timeout=20)
        except HTTPError as exc:
            response = exc
        with response:
            require(response.status == expected, "log_content_http_status")
            raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024, "log_content_budget")
            http.requests += 1
            if expected == 200:
                require(
                    response.headers.get_content_type() == "text/plain",
                    "log_content_type_changed",
                )
            return raw

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
        require(publication["state"] == "active", "staged_reads_not_published")
        target = context["target"]
        buckets = target.list_buckets()["Buckets"]
        require(len(buckets) == 1, "staged_target_bucket_changed")
        bucket = buckets[0]["Name"]
        matches = [
            row
            for row in context["final_rows"]
            if row["source"]["key"] in self.value["keys"]
        ]
        require(
            len(matches) == 2 and all(row["state"] == "verified" for row in matches),
            "staged_mapping_incomplete",
        )
        for row in matches:
            target.put_object(
                Bucket=bucket, Key=row["target_key"], Body=b"foreign latest"
            )
            target.delete_object(Bucket=bucket, Key=row["target_key"])
        source_id = json.loads(context["config"].read_bytes())["source_container"]
        record = next(row for row in owner.data["resources"] if row["id"] == source_id)
        owner.action(record, "stop", "--time", "2")
        require(
            not owner.verified(record)["State"]["Running"], "staged_source_not_offline"
        )
        before = object_facts(target, [bucket])
        summary["stage"] = "offline_staged_reads"
        preflight_path = f"/api/v1/projects/{self.value['project_id']}/mask-formats/imports:preflight"
        payload = {
            "format_id": "aap_json",
            "staged_object_key": self.value["staged_key"],
            "staged_sha256": self.value["staged_sha256"],
            "mapping": {},
            "options": {"overwrite": False},
        }
        preflight = http.call("POST", preflight_path, payload)
        require(preflight["plan"] == self.value["plan"], "staged_plan_changed")
        result = self.result(context)
        staged_route = next(
            row for row in matches if row["source"]["key"] == self.value["keys"][0]
        )
        require(
            result["materialized"]
            and result["business_facts_unchanged"]
            and result["body_and_sdk_closed"]
            and result["version_id"] == staged_route["receipt"]["version_id"],
            "independent_staging_read_changed",
        )
        artifact_path = (
            f"/projects/{self.value['project_id']}/training-runs/{self.value['run_id']}"
            f"/attempts/{self.value['attempt_id']}/artifacts/{self.value['log_name']}"
        )
        run_path = f"/api/v1/projects/{self.value['project_id']}/training-runs/{self.value['run_id']}"
        detail = http.call("GET", run_path)
        require(
            detail["run"]["config"] == self.value["run_config"]
            and detail["run"]["status"] == "failed"
            and detail["run"]["current_attempt_id"] == self.value["attempt_id"]
            and any(
                attempt["id"] == self.value["attempt_id"]
                and attempt["artifacts"][self.value["log_name"]]
                == self.value["artifact"]
                for attempt in detail["attempts"]
            ),
            "terminal_log_detail_changed",
        )
        recovery = http.call("GET", run_path + "/recovery")
        require(
            recovery["eligible"] is False
            and recovery["attempt_id"] == self.value["attempt_id"],
            "fixture_unexpected_training_recovery",
        )
        descriptor_path = "/api/v1" + artifact_path + "/download"
        descriptor = http.call("GET", descriptor_path)
        require(
            descriptor.get("requires_authentication") is True
            and descriptor.get("source_sha256") == self.value["artifact"]["sha256"]
            and descriptor.get("url") == artifact_path + "/content"
            and type(descriptor.get("redaction_revision")) is int
            and descriptor["redaction_revision"] >= 1,
            "redacted_descriptor_changed",
        )
        raw = self.content(http, descriptor["url"])
        require(
            len(raw) == descriptor["size_bytes"]
            and sha256(raw) == descriptor["sha256"]
            and descriptor["sha256"] != self.value["artifact"]["sha256"]
            and b"owned safe log line" in raw
            and b"owned-private" not in raw
            and b"[credential redacted]" in raw
            and b"[URL redacted]" in raw
            and b"[path redacted]" in raw,
            "log_redaction_or_identity_changed",
        )
        token, http.token = http.token, None
        try:
            http.call("GET", descriptor_path, expected=401)
            self.content(http, descriptor["url"], expected=401)
            http.call("POST", preflight_path, payload, expected=401)
        finally:
            http.token = token
        foreign = artifact_path.replace(
            self.value["project_id"], self.value["other_project_id"], 1
        )
        http.call("GET", "/api/v1" + foreign + "/download", expected=404)
        self.content(http, foreign + "/content", expected=404)
        require(object_facts(target, [bucket]) == before, "read_created_s3_artifacts")
        if context["args"].pause_for_browser:
            summary["stage"] = "staged_browser_pause"
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
                extra={
                    **{
                        name: self.value[name]
                        for name in ("project_id", "run_id", "attempt_id", "log_name")
                    },
                    "project_path": (
                        f"/projects/{self.value['project_id']}/learning/experiments/"
                        f"{self.value['run_id']}?tab=training&attempt={self.value['attempt_id']}"
                    ),
                },
            )
            require(
                object_facts(target, [bucket]) == before,
                "browser_read_created_s3_artifacts",
            )
        summary["stage"] = "withdraw_staged_reads"
        pending_id = str(
            uuid.UUID(
                self.child(
                    owner,
                    context["environment"],
                    "from app.workers.celery_app import celery_app\n"
                    + f"print(celery_app.send_task({self.hold_task_name!r},queue='default').id)\n",
                )
            )
        )
        try:
            deadline = time.monotonic() + 20
            while not Path(self.value["read_started"]).is_file():
                require(time.monotonic() < deadline, "staging_physical_get_not_started")
                time.sleep(0.05)
            pending = op(
                "withdraw-reads", expected_revision=publication["revision"], expected=2
            )
            require(
                pending.get("code") == "read_publication_unsettled",
                "staging_withdrawal_did_not_wait",
            )
            current = op("status-reads")["item"]
            require(
                current["state"] == "withdrawing", "staging_withdrawing_not_durable"
            )
        finally:
            private_write(self.value["read_continue"], b"owned")
        denied = self.result(context, task_id=pending_id)
        require(
            denied["denied_after_withdrawal"]
            and denied["body_and_sdk_closed"]
            and denied["temporary_directory_removed"]
            and denied["business_facts_unchanged"]
            and denied["version_id"] == staged_route["receipt"]["version_id"],
            "withdrawn_staging_read_delivered",
        )
        withdrawn = op("withdraw-reads", expected_revision=current["revision"])["item"]
        require(withdrawn["state"] == "withdrawn", "staged_publication_not_withdrawn")
        http.call("POST", preflight_path, payload, expected=409)
        http.call("GET", descriptor_path, expected=409)
        self.content(http, descriptor["url"], expected=409)
        require(object_facts(target, [bucket]) == before, "staged_target_facts_changed")
        summary.update(
            stage="complete",
            source_offline=True,
            foreign_latest_and_marker=True,
            authenticated_mask_preflight=True,
            saved_mask_plan_unchanged=True,
            independent_celery_saved_batch_materialize=True,
            full_mask_import_executed=False,
            terminal_log_descriptor_and_authenticated_text=True,
            terminal_log_detail_and_stopped_recovery_http=True,
            terminal_log_redaction_and_sha_verified=True,
            unauthenticated_and_foreign_project_denied=True,
            no_s3_derivative_or_version_created=True,
            business_owner_ref_plan_artifact_unchanged=True,
            real_open_staging_get_withdrawal_waited=True,
            withdrawn_staging_delivery_denied=True,
            actual_body_sdk_and_temp_directory_closed=True,
            migration_ready=False,
            http_completed_requests=http.requests,
        )


if __name__ == "__main__":
    from verify_storage_migration_finalization import main

    raise SystemExit(main([*sys.argv[1:], "--staged-reads"]))
