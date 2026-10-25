"""Canonical video facts and physically owned, fully verified input descriptors.

Callers supply current business authority and release their transaction before
opening a source. Read sessions end before native I/O. The two admissions cover
at most 10 GiB of staged originals and remain held through probe and real close.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context
from copy import deepcopy
from dataclasses import asdict, fields
from datetime import datetime
from functools import partial
import math
import json
import re
import threading
import time
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import managed_original_reader as reader
from app.services import (
    original_sources,
    prediction_originals as common,
    storage_access,
    task_originals,
)
from app.services.content_snapshot import ContentError
from app.services.exporting import video_probe
from app.services.reference_keys import is_reference

SOURCE_CONTRACT = 3
MAX_BYTES = 5 * 1024**3
MAX_LINKS = common.MAX_LINKS
MAX_PROOF_BYTES = common.MAX_PROOF_BYTES
COPY_SECONDS = 300
_slots = threading.BoundedSemaphore(2)
_SHA = re.compile(r"[0-9a-f]{64}")
_FIELDS = {
    "schema",
    "media_type",
    "owner_kind",
    "content_id",
    "sha256",
    "size_bytes",
    "location_id",
    "location_revision",
    "maintenance_revision",
    "endpoint_url",
    "bucket",
    "object_key",
    "facts_digest",
    "facts",
}


def _reject(code="prediction_video_original_unsupported", status=409):
    raise ContentError(code, status)


def _uuid(value, *, optional=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("Noncanonical UUID")


def _json_proof(value):
    # Persisted proof is JSON already. UUID/datetime coercion is reserved for
    # source_receipt's trusted dataclasses, not for a delivered proof document.
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > MAX_PROOF_BYTES:
        _reject("prediction_proof_budget_exceeded", 413)
    return json.loads(raw)


def _fact(value, names):
    if not isinstance(value, dict) or set(value) != set(names):
        raise ValueError("Invalid source facts")
    for name, member in value.items():
        if name == "id" or (name.endswith("_id") and name != "version_id"):
            _uuid(member, optional=name in {"connection_id", "creation_id"})
        if name in {"size_bytes", "config_revision", "width", "height"}:
            if member is not None and type(member) is not int:
                raise ValueError("Invalid numeric fact")
        if name in {"enabled", "use_ssl"} and type(member) is not bool:
            raise ValueError("Invalid boolean fact")
        if name in {"created_at", "verified_at"} and member is not None:
            if (
                not isinstance(member, str)
                or datetime.fromisoformat(member).utcoffset() is None
            ):
                raise ValueError("Invalid timestamp fact")


def _ownership(facts, value, relation):
    location = facts["location_facts"]
    _fact(location, (field.name for field in fields(original_sources.LocationFacts)))
    if (
        location["id"] != value["location_id"]
        or location["config_revision"] != value["location_revision"]
        or location["enabled"] is not True
        or location["kind"] != "s3"
        or f"{'https' if location['use_ssl'] else 'http'}://{location['endpoint']}"
        != value["endpoint_url"]
    ):
        raise ValueError
    ownership = facts["ownership_facts"]
    if value["owner_kind"] == "task_original":
        _fact(ownership, task_originals.FACTS)
        if (
            facts["item_id"] is not None
            or facts["item_facts"] is not None
            or facts["replica_facts"] is not None
            or relation["original_id"] != value["content_id"]
            or ownership["id"] != value["content_id"]
            or ownership["creation_id"] != value["content_id"]
            or ownership["task_id"] != relation["task_id"]
            or ownership["project_id"] != relation["project_id"]
        ):
            raise ValueError
    else:
        _fact(ownership, (field.name for field in fields(original_sources.AssetFacts)))
        item, replica = facts["item_facts"], facts["replica_facts"]
        _fact(item, (field.name for field in fields(original_sources.ItemFacts)))
        _fact(replica, (field.name for field in fields(original_sources.ReplicaFacts)))
        if (
            facts["item_id"] is None
            or relation["original_id"] is not None
            or ownership["id"] != value["content_id"]
            or ownership["item_id"] != facts["item_id"]
            or ownership["media_type"] != "video"
            or item["id"] != facts["item_id"]
            or item["file_path"] != value["object_key"]
            or item["file_type"] != "video"
            or replica["asset_id"] != value["content_id"]
        ):
            raise ValueError
        for name in ("location_id", "bucket", "object_key"):
            if replica[name] != value[name]:
                raise ValueError
    for name in ("sha256", "size_bytes"):
        if ownership[name] != value[name]:
            raise ValueError
    if value["owner_kind"] == "task_original":
        for name in ("location_id", "bucket", "object_key"):
            if ownership[name] != value[name]:
                raise ValueError


def normalize_binding(value):
    """Validate one private schema-three source receipt, never upgrade old proof."""
    try:
        value = _json_proof(value)
        if (
            not isinstance(value, dict)
            or set(value) != _FIELDS
            or type(value["schema"]) is not int
            or value["schema"] != SOURCE_CONTRACT
            or value["media_type"] != "video"
            or value["owner_kind"] not in {"dataset_asset", "task_original"}
            or any(
                type(value[name]) is not int or value[name] < 1
                for name in ("size_bytes", "location_revision", "maintenance_revision")
            )
            or value["size_bytes"] > MAX_BYTES
        ):
            raise ValueError
        for name in ("content_id", "location_id"):
            _uuid(value[name])
        reader.ObjectSpec(
            value["endpoint_url"],
            value["bucket"],
            value["object_key"],
            value["sha256"],
            value["size_bytes"],
        )
        if is_reference(value["object_key"]):
            raise ValueError
        facts = value["facts"]
        if (
            not isinstance(facts, dict)
            or set(facts)
            != {field.name for field in fields(original_sources.OriginalSource)}
            or any(
                facts[name] != value[name]
                for name in (
                    "owner_kind",
                    "content_id",
                    "sha256",
                    "size_bytes",
                    "media_type",
                    "location_id",
                    "location_revision",
                    "maintenance_revision",
                    "endpoint_url",
                    "bucket",
                    "object_key",
                )
            )
            or any(
                type(facts[name]) is not int
                for name in (
                    "size_bytes",
                    "location_revision",
                    "maintenance_revision",
                )
            )
            or not isinstance(value["facts_digest"], str)
            or _SHA.fullmatch(value["facts_digest"]) is None
            or common.digest(facts) != value["facts_digest"]
        ):
            raise ValueError
        _uuid(facts["item_id"], optional=True)
        relation = facts["relation"]
        if (
            not isinstance(relation, dict)
            or set(relation)
            != {field.name for field in fields(original_sources.TaskRelation)}
            or relation["file_type"] != "video"
            or relation["file_path"] != value["object_key"]
            or relation["item_id"] != facts["item_id"]
            or not isinstance(relation["links"], list)
            or len(relation["links"]) > MAX_LINKS
        ):
            raise ValueError
        for name in ("task_id", "project_id"):
            _uuid(relation[name])
        for name in (
            "batch_id",
            "original_id",
            "direct_item_id",
            "primary_item_id",
            "primary_link_id",
            "item_id",
        ):
            _uuid(relation[name], optional=True)
        for link in relation["links"]:
            if (
                not isinstance(link, dict)
                or set(link)
                != {field.name for field in fields(original_sources.LinkFact)}
                or link["task_id"] != relation["task_id"]
                or not isinstance(link["role"], str)
                or not link["role"]
                or (
                    link["sensor_name"] is not None
                    and not isinstance(link["sensor_name"], str)
                )
            ):
                raise ValueError
            for name in ("id", "task_id", "item_id"):
                _uuid(link[name])
        _ownership(facts, value, relation)
    except ContentError as exc:
        if exc.code == "prediction_proof_budget_exceeded":
            raise
        _reject("prediction_video_original_invalid")
    except (TypeError, ValueError, KeyError, OverflowError, AttributeError):
        _reject("prediction_video_original_invalid")
    return value


def source_receipt(source):
    if (
        not isinstance(source, original_sources.OriginalSource)
        or source.owner_kind not in {"dataset_asset", "task_original"}
        or source.media_type != "video"
        or source.relation is None
        or source.location_id is None
        or type(source.size_bytes) is not int
        or not 0 < source.size_bytes <= MAX_BYTES
        or is_reference(source.object_key)
    ):
        _reject()
    facts = common.bounded_proof(asdict(source))
    return normalize_binding(
        {
            "schema": SOURCE_CONTRACT,
            "media_type": "video",
            **{
                name: facts[name]
                for name in (
                    "owner_kind",
                    "content_id",
                    "sha256",
                    "size_bytes",
                    "location_id",
                    "location_revision",
                    "maintenance_revision",
                    "endpoint_url",
                    "bucket",
                    "object_key",
                )
            },
            "facts_digest": common.digest(facts),
            "facts": facts,
        }
    )


async def resolve(db, task_id, *, lock=False):
    source = await original_sources.CachedResolver(
        db, lock=lock, max_links=MAX_LINKS
    ).task(task_id)
    source_receipt(source)
    return source


async def managed_binding(db, task, *, lock=False):
    """The caller's outer Task binding, not this source receipt, owns Task.version."""
    return source_receipt(await resolve(db, task.id, lock=lock))


def normalize_probe(value):
    """Keep the H2 receipt shape and its actual decoded frame-count boundary."""
    try:
        value = _json_proof(value)
        video = value["video"]
        if (
            set(value) != {"probe_contract", "video"}
            or type(value["probe_contract"]) is not int
            or value["probe_contract"] != video_probe.PROBE_CONTRACT
            or not isinstance(video, dict)
            or set(video)
            != {"duration_ms", "fps", "frame_count", "width", "height", "codec"}
            or any(
                type(video[name]) is not int or video[name] < 1
                for name in ("frame_count", "width", "height")
            )
            or video["frame_count"] > video_probe.MAX_SOURCE_FRAMES
            or type(video["fps"]) not in (int, float)
            or not math.isfinite(video["fps"])
            or video["fps"] <= 0
            or (
                video["duration_ms"] is not None
                and (type(video["duration_ms"]) is not int or video["duration_ms"] < 0)
            )
            or not isinstance(video["codec"], str)
            or not 0 < len(video["codec"]) <= 128
        ):
            raise ValueError
    except (TypeError, KeyError, ValueError, OverflowError):
        _reject("prediction_video_probe_invalid", 422)
    return value


class _Admission:
    def __init__(self):
        if not _slots.acquire(blocking=False):
            _reject("snapshot_capacity_exceeded", 429)
        self.retained = self.released = False

    def release(self):
        # BinaryStage calls this only after real FD close. Permit settlement and
        # unknown provider/process state remain the enclosing owner's duty.
        pass

    def finish(self, unsettled):
        self.retained = self.retained or unsettled
        if not self.retained and not self.released:
            self.released = True
            _slots.release()


@asynccontextmanager
async def _session(db):
    owned = AsyncSession(
        bind=storage_access.engine(db), expire_on_commit=False, autoflush=False
    )
    try:
        yield owned
    finally:
        # A cancelled __aexit__ cannot leave SQL locks over physical I/O.
        await common.settled(owned.close())


class SourceInput:
    """One source lifetime; an unknown close cannot become a second no-op success."""

    def __init__(self, stage, probe, permit, admission):
        self.stage, self.permit = stage, permit
        self._probe = deepcopy(probe)
        self._admission = admission
        self._close_task = None
        self._closed = self._settled = self._unsettled = False

    @property
    def snapshot(self):
        return self.stage

    @property
    def probe(self):
        return deepcopy(self._probe)

    @property
    def closed(self):
        return self._closed

    @property
    def settled(self):
        return self._settled

    @property
    def unsettled(self):
        return self._unsettled

    async def _finish(self):
        failure = None
        try:
            if self.stage is not None:
                await common.physical_close(self.stage.close)
            self._closed = True
        except BaseException as exc:
            failure = exc
        if not getattr(failure, "storage_operation_unsettled", False):
            # physical_close marks every actual failure; a drained interruption
            # alone therefore still proves that the local source has closed.
            self._closed = True
        if not self._unsettled and not getattr(
            failure, "storage_operation_unsettled", False
        ):
            completed = False

            async def settle():
                nonlocal completed
                if self.permit is not None:
                    await self.permit.__aexit__(None, None, None)
                completed = True

            try:
                await common.settled(settle())
            except BaseException as exc:
                if not completed:
                    storage_access.mark_unsettled(exc)
                if failure is None:
                    failure = exc
                elif getattr(exc, "storage_operation_unsettled", False):
                    storage_access.mark_unsettled(failure)
            if completed:
                self._settled = self._closed
        self._unsettled = self._unsettled or bool(
            getattr(failure, "storage_operation_unsettled", False)
        )
        self._settled = self._settled and not self._unsettled
        self._admission.finish(self._unsettled)
        if failure is not None:
            raise failure

    async def aclose(self, failure=None):
        if getattr(failure, "storage_operation_unsettled", False):
            self._unsettled = True
            self._admission.finish(True)
        if self._unsettled:
            raise storage_access.mark_unsettled(
                ContentError("storage_operation_unsettled", 503)
            )
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._finish())
        try:
            await asyncio.shield(self._close_task)
        except BaseException as interrupted:
            try:
                await common.drain(self._close_task)
            except BaseException as later:
                if getattr(later, "storage_operation_unsettled", False):
                    storage_access.mark_unsettled(interrupted)
            raise

    async def close(self, failure=None):
        await self.aclose(failure)


async def _capture(source, admission, **read_options):
    stop = threading.Event()
    pending = asyncio.get_running_loop().run_in_executor(
        None,
        copy_context().run,
        partial(
            reader._capture,
            reader.ObjectSpec(
                source.endpoint_url,
                source.bucket,
                source.object_key,
                source.sha256,
                source.size_bytes,
            ),
            MAX_BYTES,
            COPY_SECONDS,
            time.monotonic() + COPY_SECONDS,
            stop,
            admission.release,
            transport_timeout_seconds=30,
            retain_on_close_error=True,
            **read_options,
        ),
    )
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        stop.set()
        try:
            stage = await common.drain(pending)
        except BaseException as later:
            if getattr(later, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(interrupted)
        else:
            try:
                await common.physical_close(stage.close)
            except BaseException as later:
                if getattr(later, "storage_operation_unsettled", False):
                    storage_access.mark_unsettled(interrupted)
        raise


async def open_snapshot(
    db, task_id, expected, *, guard, purpose="import", node_id=None
):
    """Return verified video bytes/probe; no business commit or implicit authority."""
    if not callable(guard):
        _reject("prediction_video_guard_required")
    expected = normalize_binding(expected)

    async def fresh():
        async with _session(db) as owned:
            await guard(owned)
            source = await resolve(owned, task_id, lock=True)
            # Canonical JSON equality also distinguishes booleans from integers.
            if common.encoded(source_receipt(source)) != common.encoded(expected):
                raise ContentError("content_binding_changed")
            return source

    source = await common.settled(fresh())
    admission = _Admission()
    owner = SourceInput(None, None, None, admission)
    try:

        async def acquire():
            async with _session(db) as owned:
                return await storage_access.acquire(
                    owned,
                    source.location_id,
                    purpose,
                    expected_revision=source.maintenance_revision,
                    node_id=node_id,
                )

        owner.permit = await common.settled(acquire(), late=common.close_permit)
        await common.settled(fresh())
        read_options = await common.settled(original_sources.read_options(db, source))
        owner.stage = await _capture(source, admission, **read_options)
        if owner.stage.sha256 != source.sha256 or owner.stage.size != source.size_bytes:
            raise ContentError("source_changed")
        await common.settled(fresh())
        owner._probe = normalize_probe(
            await common.physical(video_probe.inspect, owner.stage.file)
        )
        await common.settled(fresh())
        return owner
    except BaseException as failure:
        try:
            await owner.aclose(failure)
        except BaseException as closing:
            if getattr(closing, "storage_operation_unsettled", False):
                storage_access.mark_unsettled(failure)
        raise
