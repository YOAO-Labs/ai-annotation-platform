"""Saved training owners use actual overwrite deltas and drain before publication."""

import asyncio
from datetime import datetime, timezone
import hashlib
import io
import json
from types import SimpleNamespace
import uuid

from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.hooks import HierarchicalEmitter
import pytest
from sqlalchemy import select

from app.config import settings
from app.db.models.storage_usage import StorageUsageMutation as Mutation
from app.services import (
    active_learning as active,
    active_learning_storage,
    sample_evaluations as reports,
    sample_evaluation_storage,
    storage,
    training_observability as logs,
    training_run_storage,
    training_storage_io as transport,
    training_write_accounting as accounting,
)
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_s3_target import Target
from tests.test_auxiliary_training_storage import make_case
from tests.test_storage_usage_accounting import accounting_factory as accounting_factory

KINDS = ("active", "report", "log")
PRODUCERS = {
    "active": "active_learning_media",
    "report": "sample_evaluation_reports",
    "log": "training_redacted_logs",
}


def endpoint():
    return (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )


class Provider:
    def __init__(self):
        self.meta = SimpleNamespace(
            endpoint_url=endpoint(),
            config=Config(retries={"max_attempts": 3}),
            events=HierarchicalEmitter(),
        )
        self.objects, self.bodies, self.puts = {}, [], []
        self.closed, self.hooks, self.sends = False, True, 1
        self.ack, self.wrong_version, self.fail_list, self.fail_output_read = (
            "actual",
            False,
            False,
            False,
        )
        self.after_put = self.before_put = None
        self.started = self.release = None
        self.body_factory = io.BytesIO

    def seed(self, key, data, metadata=None, version=None):
        value = {
            "data": data,
            "Metadata": metadata or {},
            "version": version or uuid.uuid4().hex,
        }
        self.objects.setdefault(key, []).append(value)
        return value

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {
                    "Error": {"Code": "NoSuchKey"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        row = self.objects[Key][-1]
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": row["version"],
            "ContentLength": len(row["data"]),
            "ETag": hashlib.sha256(row["data"]).hexdigest(),
            "Metadata": row["Metadata"],
        }

    def list_object_versions(self, *, Bucket, Prefix, **kwargs):
        if self.fail_list:
            raise OSError("no history permission")
        return {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "IsTruncated": False,
            "Versions": [
                {
                    "Key": Prefix,
                    "VersionId": row["version"],
                    "LastModified": datetime(2026, 10, 26, tzinfo=timezone.utc),
                    "IsLatest": i == len(self.objects.get(Prefix, [])) - 1,
                    "Size": len(row["data"]),
                    "ETag": hashlib.sha256(row["data"]).hexdigest(),
                }
                for i, row in enumerate(self.objects.get(Prefix, []))
            ],
        }

    def get_object(self, *, Bucket, Key, **kwargs):
        if self.fail_output_read and Key in self.puts:
            raise OSError("readback denied")
        row = self.objects[Key][-1]
        body = self.body_factory(row["data"])
        self.bodies.append(body)
        return {
            **self.head_object(Bucket=Bucket, Key=Key),
            "Body": body,
            "VersionId": "foreign"
            if self.wrong_version and Key in self.puts
            else row["version"],
        }

    def put_object(self, **params):
        if self.before_put:
            self.before_put()
        model = SimpleNamespace(name="PutObject")
        if self.hooks:
            self.meta.events.emit(
                "before-parameter-build.s3.PutObject", params=params, model=model
            )
            for _ in range(self.sends):
                self.meta.events.emit("before-send.s3.PutObject")
        if self.started:
            self.started.set()
            assert self.release.wait(5)
        row = self.seed(params["Key"], bytes(params["Body"]), params.get("Metadata"))
        self.puts.append(params["Key"])
        if self.after_put:
            self.after_put()
        response = {
            "ResponseMetadata": {"HTTPStatusCode": 200},
            "VersionId": row["version"] if self.ack == "actual" else self.ack,
        }
        if self.hooks:
            self.meta.events.emit(
                "after-call.s3.PutObject",
                parsed=response,
                model=model,
                http_response=SimpleNamespace(status_code=200),
            )
        return response

    def generate_presigned_url(self, *args, **kwargs):
        return "https://download.test/signed"

    def close(self):
        self.closed = True


def case(kind, *, bucket="saved-training"):
    if kind == "active":
        _, row = make_case(
            active_learning_storage, "active-learning", "active-learning"
        )
        png = io.BytesIO()
        from PIL import Image

        Image.new("RGB", (4, 3), (20, 50, 90)).save(png, format="PNG")
        data = png.getvalue()
        sample = {
            "ordinal": 0,
            "sha256": "a" * 64,
            "derived_sha256": hashlib.sha256(data).hexdigest(),
        }
        owner = active_learning_storage.resolve(row)
        key = owner.key("media/0.png")

        def callback(bound):
            return active._store_png(row, sample, owner, data, transport=bound)
    elif kind == "report":
        _, row = make_case(
            sample_evaluation_storage, "sample-evaluation", "sample-evaluations"
        )
        row.spec_digest = "b" * 64
        row.spec = {"benchmark_digest": "c" * 64, "model_ref": {"sha256": "d" * 64}}
        attempt = SimpleNamespace(id=uuid.uuid4(), run_id=row.id)
        report = {"status": "succeeded", "samples": []}
        data = json.dumps(report, separators=(",", ":")).encode()
        owner = sample_evaluation_storage.resolve(row)
        key = owner.key(
            str(attempt.id) + "/" + hashlib.sha256(data).hexdigest() + ".json"
        )

        def callback(bound):
            return reports.upload_report(row, attempt, report, transport=bound)
    else:
        _, row = make_case(training_run_storage, "training-run", "runs")
        owner = training_run_storage.resolve(row)
        attempt = SimpleNamespace(
            id=uuid.uuid4(), run_id=row.id, storage_ref=owner.ref, artifacts={}
        )
        source = b"token=secret\nnormal log line\n"
        artifact = {
            "role": "log",
            "key": owner.key(f"attempts/{attempt.id}/train.log"),
            "sha256": hashlib.sha256(source).hexdigest(),
            "size_bytes": len(source),
        }
        # Obtain the exact unchanged redaction bytes from the same line engine.
        redactor = logs.RedactedLines((settings.training_executor_token,))
        data = "".join(
            [*redactor.feed(source), *redactor.feed(b"", final=True)]
        ).encode()
        key = owner.key(
            f"attempts/{attempt.id}/redacted-logs/train.log.{hashlib.sha256(data).hexdigest()}.log"
        )

        def callback(bound):
            return logs.redacted_log(
                row, attempt, "train.log", artifact, transport=bound
            )

    # Keep source identity stable while choosing only this task's bucket.
    from app.services.managed_original_retirement import managed_storage_identity

    row.storage_ref = {
        **row.storage_ref,
        "bucket": bucket,
        "storage_identity": managed_storage_identity(endpoint(), bucket),
    }
    owner = {
        "active": active_learning_storage,
        "report": sample_evaluation_storage,
        "log": training_run_storage,
    }[kind].resolve(row)
    if kind == "log":
        attempt.storage_ref = owner.ref
    target = Target(
        LEGACY_LOCATION_ID, 1, None, endpoint(), bucket, "", purpose="training"
    )
    return SimpleNamespace(
        kind=kind,
        row=row,
        owner=owner,
        target=target,
        key=key,
        data=data,
        callback=callback,
        attempt=locals().get("attempt"),
        source=locals().get("source"),
        artifact=locals().get("artifact"),
    )


def install(monkeypatch, provider, value):
    shared = SimpleNamespace(
        meta=provider.meta,
        head_object=provider.head_object,
        get_object=provider.get_object,
    )
    value.target = Target(
        LEGACY_LOCATION_ID,
        1,
        None,
        endpoint(),
        value.owner.bucket,
        "",
        purpose="training",
        _legacy_client=shared,
    )
    monkeypatch.setattr(transport.storage_service, "client", shared)
    configs = []

    def create(*, client_config):
        assert client_config is provider.meta.config
        configs.append(client_config)
        return SimpleNamespace(client=provider)

    monkeypatch.setattr(storage, "StorageService", create)
    if value.kind == "log":
        provider.seed(
            value.artifact["key"],
            value.source,
            {"attempt-id": str(value.attempt.id), "sha256": value.artifact["sha256"]},
        )
    return configs


async def rows(factory):
    async with factory() as db:
        return list(
            await db.scalars(
                select(Mutation).order_by(Mutation.started_at, Mutation.id)
            )
        )


async def physical(factory, value):
    async with factory() as db:
        rec = accounting.recorder(
            db,
            value.owner,
            value.row.id,
            producer=PRODUCERS[value.kind],
            purpose="other_platform",
        )
    failure = None
    try:
        await asyncio.to_thread(
            transport.physical, value.target, value.callback, accounting=rec
        )
    except BaseException as exc:
        failure = exc
    return await accounting.finish(rec, failure)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("old_size", [None, 0, "equal", "larger"])
async def test_actual_writer_overwrites_prove_net_delta(
    accounting_factory, monkeypatch, kind, old_size
):
    value = case(kind)
    provider = Provider()
    configs = install(monkeypatch, provider, value)
    old = (
        0
        if old_size is None
        else len(value.data)
        if old_size == "equal"
        else len(value.data) + 9
        if old_size == "larger"
        else old_size
    )
    if old_size is not None:
        provider.seed(value.key, b"x" * old)
    assert await physical(accounting_factory, value) is None
    (fact,) = await rows(accounting_factory)
    assert (
        fact.state == "confirmed" and fact.current_delta_bytes == len(value.data) - old
    )
    assert fact.owner_id == value.row.id and fact.purpose == "other_platform"
    assert provider.closed and configs and all(body.closed for body in provider.bodies)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize(
    "uncertain",
    [
        "retry",
        "no_hooks",
        "null",
        "missing",
        "lost_ack",
        "wrong_get",
        "no_list",
        "client_close",
    ],
)
async def test_unknown_proof_never_counts_full_upload_size(
    accounting_factory, monkeypatch, kind, uncertain
):
    value = case(kind)
    provider = Provider()
    install(monkeypatch, provider, value)
    provider.seed(value.key, b"old bytes")
    if uncertain == "retry":
        provider.sends = 2
    elif uncertain == "no_hooks":
        provider.hooks = False
    elif uncertain in {"null", "missing"}:
        provider.ack = "null" if uncertain == "null" else None
    elif uncertain == "lost_ack":
        provider.after_put = lambda: (_ for _ in ()).throw(OSError("ACK lost"))
    elif uncertain == "wrong_get":
        provider.wrong_version = True
    elif uncertain == "no_list":
        provider.fail_list = True
    else:

        def close():
            provider.closed = True
            raise OSError("client close unknown")

        provider.close = close
    failure = await physical(accounting_factory, value)
    assert (failure is not None) == (uncertain in {"lost_ack", "client_close"})
    (fact,) = await rows(accounting_factory)
    assert fact.state == "unknown" and fact.current_delta_bytes is None


async def test_redacted_observational_read_failure_preserves_business_success(
    accounting_factory, monkeypatch
):
    value = case("log")
    provider = Provider()
    install(monkeypatch, provider, value)
    provider.fail_output_read = True
    assert await physical(accounting_factory, value) is None
    (fact,) = await rows(accounting_factory)
    assert fact.state == "unknown" and fact.physical_outcome == "written"


@pytest.mark.parametrize("kind", KINDS)
async def test_intent_is_durable_before_put_and_ordinary_publish_failure_is_independent(
    accounting_factory, monkeypatch, kind
):
    from app.services import media_cache_accounting

    value = case(kind)
    provider = Provider()
    install(monkeypatch, provider, value)
    original = media_cache_accounting.Recorder._begin

    async def unavailable(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", unavailable)
    assert isinstance(await physical(accounting_factory, value), OSError)
    assert not provider.puts and not await rows(accounting_factory)
    monkeypatch.setattr(media_cache_accounting.Recorder, "_begin", original)
    loop = asyncio.get_running_loop()

    async def pending():
        (fact,) = await rows(accounting_factory)
        assert fact.state == "pending" and fact.owner_id == value.row.id
        assert fact.evidence["expected_size_bytes"] == len(value.data)
        assert (
            fact.evidence["expected_sha256"] == hashlib.sha256(value.data).hexdigest()
        )

    provider.before_put = lambda: asyncio.run_coroutine_threadsafe(
        pending(), loop
    ).result(3)
    monkeypatch.setattr(media_cache_accounting.accounting, "publish", unavailable)
    assert await physical(accounting_factory, value) is None
    (fact,) = await rows(accounting_factory)
    assert fact.state == "pending" and fact.current_delta_bytes is None
