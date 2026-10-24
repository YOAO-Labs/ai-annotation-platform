"""Own NuScenes sensor originals without granting authority from JSON locators.

Callers authorize the exact Task and own transaction/physical admission. Creation
receipts remain historical; this current owner is the offline restore anchor.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import make_dataclass
from datetime import datetime
import json
import re
import uuid

from sqlalchemy import Text, case, cast, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError

from app.db.models.dataset import DatasetItem, Scene
from app.db.models.dataset_auxiliary_original import (
    DatasetAuxiliaryOriginal as Original,
)
from app.services import managed_original_creation as creation
from app.services import original_sources as sources
from app.services import storage_access, task_originals
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

PREFIX = "__aap_auxiliary_originals__"
MAX_BYTES = 256 * 1024**2
MAX_CONTRACT_BYTES = 65536
RAW_FIELDS = ("sample", "sample_data", "calibrated_sensor", "sensor", "ego_pose")
FACTS = (
    "id",
    "creation_id",
    "dataset_id",
    "kind",
    "source_contract",
    "location_id",
    "storage_identity",
    "bucket",
    "object_key",
    "sha256",
    "size_bytes",
    "etag",
    "version_id",
    "created_at",
)
_SHA = re.compile(r"[0-9a-f]{64}")
_Snapshot = make_dataclass(
    "AuxiliaryOriginalSnapshot", [(key, object) for key in FACTS], frozen=True
)


def reject(code="invalid_receipt", status=409):
    raise ContentError("auxiliary_original_" + code, status)


def _get(row, key):
    return row.get(key) if isinstance(row, Mapping) else getattr(row, key, None)


def contract(kind, value):
    """Validate the bounded original table contract, never a storage locator."""
    if kind not in {"raw_lidar", "map"} or not isinstance(value, dict):
        reject("contract_invalid")
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        reject("contract_invalid")
    if len(encoded.encode()) > MAX_CONTRACT_BYTES:
        reject("source_budget_exceeded", 422)
    if kind == "raw_lidar":
        if set(value) != set(RAW_FIELDS) or any(
            not isinstance(value[key], dict) for key in RAW_FIELDS
        ):
            reject("contract_invalid")
        sample, data, calibrated, sensor, pose = (value[key] for key in RAW_FIELDS)
        if (
            not all(
                row.get("token") for row in (sample, data, calibrated, sensor, pose)
            )
            or data.get("sample_token") != sample["token"]
            or data.get("calibrated_sensor_token") != calibrated["token"]
            or calibrated.get("sensor_token") != sensor["token"]
            or data.get("ego_pose_token") != pose["token"]
            or data.get("timestamp") != pose.get("timestamp")
            or data.get("is_key_frame") is not True
            or sensor.get("modality") != "lidar"
            or not isinstance(data.get("filename"), str)
            or not data["filename"].endswith(".pcd.bin")
        ):
            reject("contract_invalid")
    elif (
        not value.get("token")
        or not value.get("filename")
        or not isinstance(value.get("log_tokens"), list)
        or not value["log_tokens"]
        or not all(isinstance(key, str) and key for key in value["log_tokens"])
    ):
        reject("contract_invalid")
    return json.loads(encoded)


def raw_contract(metadata):
    source = (metadata or {}).get("nuscenes_export")
    if not isinstance(source, dict):
        reject("contract_invalid")
    return contract("raw_lidar", {key: source.get(key) for key in RAW_FIELDS})


def map_contract(metadata):
    source = (metadata or {}).get("nuscenes_export")
    if not isinstance(source, dict):
        reject("contract_invalid")
    return contract("map", source.get("map"))


def key_for(dataset_id, identifier):
    if not isinstance(dataset_id, uuid.UUID) or not isinstance(identifier, uuid.UUID):
        reject()
    return f"{PREFIX}/{dataset_id}/{identifier}/payload"


def resolve(row, *, endpoint_url=None):
    if row is None:
        reject("unpinned")
    identifiers = [
        _get(row, name) for name in ("id", "creation_id", "dataset_id", "location_id")
    ]
    if (
        any(not isinstance(value, uuid.UUID) for value in identifiers)
        or identifiers[0] != identifiers[1]
        or identifiers[3] != LEGACY_LOCATION_ID
        or _get(row, "object_key") != key_for(identifiers[2], identifiers[0])
        or not isinstance(_get(row, "sha256"), str)
        or not _SHA.fullmatch(_get(row, "sha256"))
        or type(_get(row, "size_bytes")) is not int
        or not 0 <= _get(row, "size_bytes") <= MAX_BYTES
        or not task_originals._safe(_get(row, "etag"), 1024)
        or (
            _get(row, "version_id") is not None
            and (
                not task_originals._safe(_get(row, "version_id"), 1024)
                or _get(row, "version_id") == "null"
            )
        )
    ):
        reject()
    timestamp = _get(row, "created_at")
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        reject()
    contract(_get(row, "kind"), _get(row, "source_contract"))
    if _get(row, "kind") == "raw_lidar" and _get(row, "size_bytes") % 20:
        reject("contract_invalid")
    bucket, identity = _get(row, "bucket"), _get(row, "storage_identity")
    if not task_originals._safe(bucket, 255, spaces=False) or any(
        c in bucket for c in "/\\?#@"
    ):
        reject("invalid_location")
    from app.config import settings

    try:
        endpoint = task_originals._endpoint(
            endpoint_url
            if endpoint_url is not None
            else f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
        )
    except ContentError:
        reject("invalid_location")
    if identity != managed_storage_identity(endpoint, bucket):
        reject("invalid_location")
    return task_originals.Locator(
        LEGACY_LOCATION_ID, identity, bucket, _get(row, "object_key"), endpoint
    )


def snapshot(row, *, endpoint_url=None):
    resolve(row, endpoint_url=endpoint_url)
    values = [_get(row, key) for key in FACTS]
    values[FACTS.index("source_contract")] = json.dumps(
        _get(row, "source_contract"), sort_keys=True, allow_nan=False
    )
    return _Snapshot(*values)


async def _persist(db, values):
    async with storage_access.engine(db).begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
        table = Original.__table__
        await connection.execute(
            insert(table)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[table.c.id])
        )
        row = (
            (await connection.execute(select(table).where(table.c.id == values["id"])))
            .mappings()
            .one()
        )
        if any(row[key] != values[key] for key in FACTS):
            reject("creation_conflict")
        return dict(row)


async def publish(db, allocation, created, *, kind, source_contract):
    """Commit saved responsibility independently; never bind or commit business rows."""
    frozen = contract(kind, source_contract)
    row = await creation._loaded(db, allocation)
    if (
        row.get("owner_kind") != "dataset_auxiliary"
        or row.get("status") != "verified"
        or (row.get("authority") or {}).get("auxiliary")
        != {"kind": kind, "source_contract": frozen}
        or any(
            getattr(created, name, None) != row[field]
            for name, field in (
                ("creation_id", "id"),
                ("sha256", "sha256"),
                ("size_bytes", "size_bytes"),
                ("md5", "md5"),
                ("etag", "etag"),
                ("version_id", "version_id"),
            )
        )
    ):
        reject("creation_conflict")
    values = {
        key: row[key]
        for key in FACTS
        if key not in {"kind", "source_contract", "creation_id", "created_at"}
    }
    values.update(
        kind=kind,
        source_contract=frozen,
        creation_id=row["id"],
        created_at=row["completed_at"],
    )
    resolve(values)
    operation = asyncio.create_task(_persist(db, values))
    try:
        saved = await asyncio.shield(operation)
    except BaseException as exc:
        try:
            await creation._drain(operation)
        except BaseException:
            pass
        if isinstance(exc, DBAPIError):
            reject(
                "publish_busy"
                if storage_access.unavailable(exc)
                else "publish_unknown",
                503,
            )
        raise
    return Original(**saved)


def _matches(owner, dataset_id, kind, expected):
    resolve(owner)
    if (
        owner.dataset_id != dataset_id
        or owner.kind != kind
        or owner.source_contract != expected
    ):
        reject("contract_changed")


def _receipt_contract(metadata, owner, kind):
    source = (metadata or {}).get("nuscenes_export") or {}
    prefix = "source" if kind == "raw_lidar" else "map"
    key = "source_storage_key" if kind == "raw_lidar" else "map_storage_key"
    if (
        source.get(key) != _get(owner, "object_key")
        or source.get(prefix + "_sha256") != _get(owner, "sha256")
        or type(source.get(prefix + "_file_size")) is not int
        or source[prefix + "_file_size"] != _get(owner, "size_bytes")
    ):
        reject("contract_changed")


async def bind_raw(db, item, owner):
    _matches(owner, item.dataset_id, "raw_lidar", raw_contract(item.metadata_))
    if item.file_type != "point_cloud" or item.nuscenes_raw_original_id not in (
        None,
        owner.id,
    ):
        reject("contract_changed")
    _receipt_contract(item.metadata_, owner, "raw_lidar")
    item.nuscenes_raw_original_id = owner.id
    await db.flush()


async def bind_map(db, scene, owner):
    _matches(owner, scene.dataset_id, "map", map_contract(scene.source_metadata))
    if scene.nuscenes_map_original_id not in (None, owner.id):
        reject("contract_changed")
    _scene_contract(scene.source_metadata, owner.source_contract)
    _receipt_contract(scene.source_metadata, owner, "map")
    scene.nuscenes_map_original_id = owner.id
    await db.flush()


def _bounded(column, name):
    return case(
        (func.octet_length(cast(column, Text)) <= MAX_CONTRACT_BYTES * 2, column),
        else_=None,
    ).label(name)


async def _owner(db, identifier, lock):
    if identifier is None:
        reject("unpinned")
    fields = [
        _bounded(Original.source_contract, "source_contract")
        if name == "source_contract"
        else case(
            (func.octet_length(Original.etag) <= 1024, Original.etag), else_=None
        ).label("etag")
        if name == "etag"
        else getattr(Original, name)
        for name in FACTS
    ]
    row = (
        (
            await db.execute(
                sources._statement(
                    select(*fields).where(Original.id == identifier), lock
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is not None and row["source_contract"] is None:
        reject("source_budget_exceeded", 422)
    return row


def _scene_contract(metadata, expected_map):
    source = (metadata or {}).get("nuscenes_export")
    if not isinstance(source, dict) or metadata.get("frame") != "ego":
        reject("contract_changed")
    scene, log = source.get("scene"), source.get("log")
    if (
        not isinstance(scene, dict)
        or not isinstance(log, dict)
        or not scene.get("token")
        or not log.get("token")
        or scene.get("log_token") != log["token"]
        or log["token"] not in expected_map.get("log_tokens", [])
        or source.get("map") != expected_map
    ):
        reject("contract_changed")
    return source


async def _resolve_task(db, task_id, kind, lock):
    async with sources._reads(db, lock):
        relation = await sources._relation(
            db, task_id, lock, compact_relation=True, max_links=2
        )
        if relation.item_id is None or relation.original_id is not None:
            reject("unpinned")
        item = (
            await db.execute(
                sources._statement(
                    select(
                        DatasetItem.id,
                        DatasetItem.dataset_id,
                        DatasetItem.file_path,
                        DatasetItem.file_type,
                        DatasetItem.width,
                        DatasetItem.height,
                        DatasetItem.scene_id,
                        DatasetItem.frame_index,
                        DatasetItem.nuscenes_raw_original_id,
                        _bounded(DatasetItem.metadata_, "metadata"),
                    ).where(DatasetItem.id == relation.item_id),
                    lock,
                )
            )
        ).one_or_none()
        if (
            item is None
            or item.file_type != "point_cloud"
            or item.scene_id is None
            or item.frame_index is None
        ):
            reject("contract_changed")
        if item.metadata is None:
            reject("source_budget_exceeded", 422)
        scene = (
            await db.execute(
                sources._statement(
                    select(
                        Scene.id,
                        Scene.dataset_id,
                        Scene.source_format,
                        Scene.nuscenes_map_original_id,
                        _bounded(Scene.source_metadata, "source_metadata"),
                    ).where(Scene.id == item.scene_id),
                    lock,
                )
            )
        ).one_or_none()
        if (
            scene is None
            or scene.dataset_id != item.dataset_id
            or scene.source_format != "nuscenes"
        ):
            reject("contract_changed")
        if scene.source_metadata is None:
            reject("source_budget_exceeded", 422)
        raw = raw_contract(item.metadata)
        scene_source = _scene_contract(
            scene.source_metadata, map_contract(scene.source_metadata)
        )
        if raw["sample"].get("scene_token") != scene_source["scene"]["token"]:
            reject("contract_changed")
        pointer = (
            item.nuscenes_raw_original_id
            if kind == "raw_lidar"
            else scene.nuscenes_map_original_id
        )
        row = await _owner(db, pointer, lock)
        locator = resolve(row)
        expected = raw if kind == "raw_lidar" else map_contract(scene.source_metadata)
        if (
            row["dataset_id"] != item.dataset_id
            or row["kind"] != kind
            or row["source_contract"] != expected
        ):
            reject("contract_changed")
        _receipt_contract(
            item.metadata if kind == "raw_lidar" else scene.source_metadata, row, kind
        )
        locations = await sources._locations(
            db, [locator.location_id], lock, bounded=True
        )
        endpoint, location_facts = sources._platform_location(
            locations[0] if len(locations) == 1 else None, bounded=True
        )
        resolve(row, endpoint_url=endpoint)
        revision = await sources._gate(db, locator.location_id, lock, bounded=True)
        return sources.OriginalSource(
            "dataset_auxiliary",
            row["id"],
            row["sha256"],
            row["size_bytes"],
            kind,
            item.id,
            relation,
            row["bucket"],
            row["object_key"],
            endpoint,
            locator.location_id,
            location_facts.config_revision,
            revision,
            snapshot(row, endpoint_url=endpoint),
            sources.ItemFacts(
                item.id,
                item.dataset_id,
                item.file_path,
                item.file_type,
                item.width,
                item.height,
            ),
            sources.ReplicaFacts(
                row["id"],
                locator.location_id,
                row["bucket"],
                row["object_key"],
                kind,
                "present",
                row["creation_id"],
                row["etag"],
                row["created_at"],
            ),
            location_facts,
        )


async def resolve_task_raw(db, task_id, *, lock=False):
    return await _resolve_task(db, task_id, "raw_lidar", lock)


async def resolve_scene_map(db, task_id, *, lock=False):
    return await _resolve_task(db, task_id, "map", lock)
