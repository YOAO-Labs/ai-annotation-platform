"""Classify one bounded LIST page by saved ownership, never by bucket labels."""

from dataclasses import dataclass
from datetime import datetime
import hashlib
from importlib import import_module
import re
from types import SimpleNamespace
import uuid

from sqlalchemy import func, literal, select, union_all

from app.config import settings
from app.db.models.dataset_asset import DatasetAssetLocation, StorageLocation
from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
from app.db.models.dataset_source import DatasetSourcePreview
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.db.models.thumbnail_output import ThumbnailOutput
from app.db.models.original_cache import OriginalCacheEntry
from app.db.models.mask_format_upload import MaskFormatUpload
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.task_original import TaskOriginal
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.services.content_snapshot import ContentError
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity
from app.services.training_dataset_bridge import TrainingDatasetError

CATEGORIES = (
    "managed_original",
    "preview_cache",
    "training_version",
    "temporary",
    "other_platform",
    "unclassified",
)
SCHEMA = 1
_UUID = r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})"


@dataclass(frozen=True)
class Family:
    model: str
    service: str
    category: str
    pattern: str
    group: int
    method: str
    arguments: tuple[str, ...]
    prefix: bool = True
    identifier: str = "id"


FAMILIES = (
    Family(
        "prediction_input.PredictionInputLease",
        "prediction_inputs",
        "temporary",
        rf"^(?:(?:[^/]+/)*_aap_managed/import/)?prediction-inputs/{_UUID}/",
        1,
        "input_prefix",
        ("id",),
    ),
    Family(
        "image_pyramid.ImagePyramidOutput",
        "image_pyramid_storage",
        "preview_cache",
        rf"^(?:(?:[^/]+/)*_aap_managed/media-cache/)?image-pyramids/objects/{_UUID}/",
        1,
        "prefix_for",
        ("id",),
    ),
    Family(
        "video_derivative.VideoDerivativeOutput",
        "video_derivative_storage",
        "preview_cache",
        rf"^(?:(?:[^/]+/)*_aap_managed/media-cache/)?video-derivatives/objects/{_UUID}/",
        1,
        "prefix_for",
        ("id",),
    ),
    Family(
        "export_media_bundle.ExportMediaBundle",
        "export_media_storage",
        "temporary",
        rf"^(?:(?:[^/]+/)*_aap_managed/export/)?lidar-export-media/objects/{_UUID}/",
        1,
        "prefix_for",
        ("id",),
    ),
    Family(
        "training_dataset.TrainingDatasetVersion",
        "training_dataset_storage",
        "training_version",
        rf"^projects/{_UUID}/datasets/{_UUID}/",
        2,
        "prefix_for",
        ("project_id", "id"),
    ),
    Family(
        "training_run.TrainingRun",
        "training_run_storage",
        "other_platform",
        rf"^projects/{_UUID}/runs/{_UUID}/",
        2,
        "prefix_for",
        ("project_id", "id"),
    ),
    Family(
        "training_preview.TrainingPreview",
        "training_preview_storage",
        "preview_cache",
        rf"^projects/{_UUID}/previews/{_UUID}/",
        2,
        "prefix_for",
        ("project_id", "id"),
    ),
    Family(
        "sample_evaluation.SampleEvaluationRun",
        "sample_evaluation_storage",
        "other_platform",
        rf"^projects/{_UUID}/sample-evaluations/{_UUID}/",
        2,
        "prefix_for",
        ("project_id", "id"),
    ),
    Family(
        "active_learning.ActiveLearningRound",
        "active_learning_storage",
        "other_platform",
        rf"^projects/{_UUID}/active-learning/{_UUID}/",
        2,
        "prefix_for",
        ("project_id", "id"),
    ),
    Family(
        "avatar_upload.AvatarUpload",
        "avatar_storage",
        "other_platform",
        r"^(?:(?:[^/]+/)*_aap_managed/avatars/)?([0-9a-f]{32})\.webp$",
        1,
        "object_key_for",
        ("token",),
        False,
        "token",
    ),
    Family(
        "bug_attachment_upload.BugAttachmentUpload",
        "bug_attachment_storage",
        "other_platform",
        rf"^(?:(?:[^/]+/)*_aap_managed/bug-reports/)?bug-report-attachments/{_UUID}/{_UUID}\.[a-z]+$",
        2,
        "key_for",
        ("owner_id", "id", "mime_type"),
        False,
    ),
    Family(
        "comment_attachment_upload.CommentAttachmentUpload",
        "comment_attachment_storage",
        "other_platform",
        rf"^comment-attachments/{_UUID}/{_UUID}$",
        2,
        "key_for",
        ("annotation_id", "id"),
        False,
    ),
    Family(
        "guide_asset_upload.GuideAssetUpload",
        "guide_asset_storage",
        "other_platform",
        rf"^projects/{_UUID}/guide/{_UUID}$",
        2,
        "key_for",
        ("project_id", "id"),
        False,
    ),
    Family(
        "raster_mask_object.RasterMaskObject",
        "raster_object_storage",
        "other_platform",
        rf"^raster-masks/objects/{_UUID}\.json(?:\.gz)?$",
        1,
        "key_for",
        ("id", "storage_encoding"),
        False,
    ),
    Family(
        "export_output.ExportOutput",
        "export_storage",
        "temporary",
        rf"^(?:(?:[^/]+/)*_aap_managed/export/)?(image|video)/{_UUID}/{_UUID}/{_UUID}\.zip$",
        4,
        "key_for",
        ("media", "project_id", "job_id", "id"),
        False,
    ),
    Family(
        "audit_archive_output.AuditArchiveOutput",
        "audit_archive_storage",
        "other_platform",
        rf"^(?:(?:[^/]+/)*_aap_managed/audit-archive/)?[0-9]{{4}}/[0-9]{{2}}/{_UUID}\.jsonl\.gz$",
        1,
        "key_for",
        ("year", "month", "id"),
        False,
    ),
)


def empty():
    return {
        "schema": SCHEMA,
        "categories": {
            name: {"object_count": 0, "size_bytes": 0} for name in CATEGORIES
        },
        "conflict_object_count": 0,
    }


def endpoint():
    return (
        f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}"
    )


def _json_fields(model, names):
    return func.jsonb_build_object(
        *[value for name in names for value in (name, getattr(model, name))]
    )


def _model(path):
    module, name = path.split(".")
    return getattr(import_module("app.db.models." + module), name)


def _claim(model, category, key_column, conditions):
    return (
        select(
            literal(category).label("kind"),
            func.jsonb_build_object("key", key_column).label("data"),
        )
        .select_from(model)
        .where(*conditions)
        .distinct()
    )


def _hashed_keys(model, bucket, keys):
    # Match the existing retirement hash index and the equivalent locator indexes.
    return (
        func.md5(model.bucket) == hashlib.md5(bucket.encode()).hexdigest(),
        func.md5(model.object_key).in_(
            [hashlib.md5(key.encode()).hexdigest() for key in keys]
        ),
        model.bucket == bucket,
        model.object_key.in_(keys),
    )


def _contains(owner, key, prefix):
    try:
        if not prefix:
            return key == owner.object_key
        if isinstance(owner, dict):
            base = owner["key_prefix"]
            return (
                key.startswith(base)
                and len(key) > len(base)
                and not any(ord(char) < 32 or char == "\\" for char in key)
                and all(
                    part not in {"", ".", ".."} for part in key[len(base) :].split("/")
                )
            )
        if not key.startswith(owner.prefix):
            return False
        owner.contains(key)
        return True
    except (
        ContentError,
        TrainingDatasetError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        return False


async def classify(db, bucket, objects):
    """One SQL query, bounded candidates and no provider/content operations."""
    if len(objects) > 1000:
        raise ValueError("invalid_inventory_page")
    keys = list(dict.fromkeys(row["key"] for row in objects))
    if len(keys) != len(objects):
        raise ValueError("invalid_inventory_page")
    result = empty()
    if not objects:
        return result
    identity = managed_storage_identity(endpoint(), bucket)
    claims = {key: set() for key in keys}
    initial_location = select(StorageLocation.id).where(
        StorageLocation.id == LEGACY_LOCATION_ID,
        StorageLocation.kind == "s3",
        StorageLocation.config_revision == 1,
        StorageLocation.config
        == {"endpoint": settings.minio_endpoint, "use_ssl": settings.minio_use_ssl},
    )
    queries = [
        _claim(
            ManagedOriginalCreation,
            "managed_original",
            ManagedOriginalCreation.object_key,
            (
                ManagedOriginalCreation.storage_identity == identity,
                ManagedOriginalCreation.bucket == bucket,
                ManagedOriginalCreation.object_key.in_(keys),
            ),
        ),
        _claim(
            ManagedOriginalCreation,
            "temporary",
            ManagedOriginalCreation.staging_key,
            (
                func.coalesce(
                    ManagedOriginalCreation.staging_ref["endpoint_url"].astext,
                    ManagedOriginalCreation.configuration["endpoint"].astext,
                )
                == endpoint(),
                ManagedOriginalCreation.staging_bucket == bucket,
                ManagedOriginalCreation.staging_key.in_(keys),
            ),
        ),
        _claim(
            DatasetAssetLocation,
            "managed_original",
            DatasetAssetLocation.object_key,
            (
                *_hashed_keys(DatasetAssetLocation, bucket, keys),
                DatasetAssetLocation.location_id.in_(initial_location),
                DatasetAssetLocation.role == "managed_original",
            ),
        ),
        _claim(
            ManagedOriginalRetirement,
            "managed_original",
            ManagedOriginalRetirement.object_key,
            (
                *_hashed_keys(ManagedOriginalRetirement, bucket, keys),
                ManagedOriginalRetirement.identity_state == "verified_binding",
                ManagedOriginalRetirement.storage_identity == identity,
            ),
        ),
        _claim(
            DatasetSourcePreview,
            "preview_cache",
            DatasetSourcePreview.object_key,
            (
                *_hashed_keys(DatasetSourcePreview, bucket, keys),
                DatasetSourcePreview.location_id.in_(initial_location),
            ),
        ),
    ]
    thumbnail_key = ThumbnailOutput.storage_ref["prefix"].astext + "thumbnail.webp"
    queries.append(
        _claim(
            ThumbnailOutput,
            "preview_cache",
            thumbnail_key,
            (
                ThumbnailOutput.storage_ref["endpoint_url"].astext == endpoint(),
                ThumbnailOutput.storage_ref["bucket"].astext == bucket,
                ThumbnailOutput.storage_ref["storage_identity"].astext == identity,
                thumbnail_key.in_(keys),
            ),
        )
    )
    for model in (DatasetAssetLocation, DatasetAuxiliaryOriginal):
        queries.append(
            _claim(
                model,
                "managed_original",
                model.object_key,
                (
                    *_hashed_keys(model, bucket, keys),
                    model.storage_ref["endpoint_url"].astext == endpoint(),
                    model.storage_ref["storage_identity"].astext == identity,
                    model.storage_ref["object_key"].astext == model.object_key,
                    model.storage_ref["bucket"].astext == model.bucket,
                ),
            )
        )
    for model, field, tag in (
        (MaskFormatUpload, "logical_object_key", "mask_upload"),
        (MaskFormatImport, "staged_object_key", "mask_batch"),
    ):
        logical_key = getattr(model, field)
        ref = model.storage_ref if tag == "mask_upload" else model.staging_ref
        physical_key = ref["key_prefix"].astext + func.substring(
            logical_key, func.length(func.regexp_replace(logical_key, "[^/]+$", "")) + 1
        )
        queries.append(
            select(
                literal(tag).label("kind"),
                func.jsonb_build_object(
                    "logical_key", logical_key, "storage_ref", ref
                ).label("data"),
            ).where(
                ref["endpoint_url"].astext == endpoint(),
                ref["bucket"].astext == bucket,
                physical_key.in_(keys),
            )
        )
    for model, namespace in (
        (TaskOriginal, "__aap_task_originals__"),
        (DatasetAuxiliaryOriginal, "__aap_auxiliary_originals__"),
    ):
        identifiers = {
            uuid.UUID(match[2])
            for key in keys
            if (match := re.fullmatch(rf"{namespace}/{_UUID}/{_UUID}/payload", key))
        }
        if identifiers:
            # Restore remaps these current locators while preserving creation history.
            queries.append(
                _claim(
                    model,
                    "managed_original",
                    model.object_key,
                    (
                        model.id.in_(identifiers),
                        model.storage_identity == identity,
                        model.bucket == bucket,
                        model.object_key.in_(keys),
                    ),
                )
            )
    families = {}
    for index, family in enumerate(FAMILIES):
        matches = [re.match(family.pattern, key) for key in keys]
        ids = {match[family.group] for match in matches if match}
        if not ids:
            continue
        model = _model(family.model)
        tag = f"ref:{index}"
        fields = tuple(
            dict.fromkeys((family.identifier, "storage_ref", *family.arguments))
        )
        queries.append(
            select(
                literal(tag).label("kind"), _json_fields(model, fields).label("data")
            ).where(
                getattr(model, family.identifier).in_(
                    ids
                    if family.identifier == "token"
                    else [uuid.UUID(value) for value in ids]
                )
            )
        )
        families[tag] = family
    cache_ids = {
        uuid.UUID(match[2])
        for key in keys
        if (
            match := re.match(
                rf"^(?:(?:[^/]+/)*_aap_managed/media-cache/)?original-cache/v1/{_UUID}/[0-9a-f]{{64}}/{_UUID}(?:/payload)?$",
                key,
            )
        )
    }
    if cache_ids:
        queries.append(
            select(
                literal("cache").label("kind"),
                _json_fields(
                    OriginalCacheEntry,
                    (
                        "id",
                        "asset_id",
                        "sha256",
                        "storage_identity",
                        "bucket",
                        "storage_ref",
                    ),
                ).label("data"),
            ).where(OriginalCacheEntry.id.in_(cache_ids))
        )
    prefixes = {
        match[0]
        for key in keys
        if (match := re.match(rf"^projects/{_UUID}/(?:datasets|runs)/{_UUID}/", key))
    }
    if prefixes:
        queries.append(
            select(
                literal("training_deletion").label("kind"),
                _json_fields(TrainingStorageDeletion, ("storage_ref", "prefix")).label(
                    "data"
                ),
            ).where(
                TrainingStorageDeletion.bucket == bucket,
                TrainingStorageDeletion.prefix.in_(prefixes),
            )
        )
    for kind, data in await db.execute(union_all(*queries)):
        if kind in CATEGORIES:
            claims[data["key"]].add(kind)
            continue
        try:
            if kind in {"mask_upload", "mask_batch"}:
                from app.services import import_storage

                logical, suffix = data["logical_key"].rsplit("/", 1)
                owner = import_storage.resolve_ref(
                    data["storage_ref"], expected_prefix=logical + "/"
                )
                key = owner.key(suffix)
                if (
                    owner.ref["endpoint_url"] == endpoint()
                    and owner.bucket == bucket
                    and key in claims
                ):
                    claims[key].add("temporary")
                continue
            if kind == "cache":
                from app.services.original_cache import object_key

                key = object_key(SimpleNamespace(**data))
                if (
                    data["storage_identity"] == identity
                    and data["bucket"] == bucket
                    and key in claims
                ):
                    claims[key].add("preview_cache")
                continue
            if kind == "training_deletion":
                from app.services.training_dataset_storage import _resolve_ref

                purpose = (data["storage_ref"] or {}).get("purpose")
                if purpose not in {"training-dataset", "training-run"}:
                    continue
                owner = _resolve_ref(
                    data["storage_ref"], purpose=purpose, expected_prefix=data["prefix"]
                )
                category = (
                    "training_version"
                    if purpose == "training-dataset"
                    else "other_platform"
                )
                prefix = True
            else:
                family = families[kind]
                service = import_module("app.services." + family.service)
                expected = getattr(service, family.method)(
                    *[data[name] for name in family.arguments]
                )
                if (
                    family.service
                    in {"image_pyramid_storage", "video_derivative_storage"}
                    and (data["storage_ref"] or {}).get("schema") == 2
                ):
                    from app.services import media_cache_storage

                    owner = media_cache_storage.resolve_ref(
                        data["storage_ref"], expected_prefix=expected
                    )
                else:
                    owner = service.resolve_ref(
                        data["storage_ref"],
                        **{
                            "expected_prefix"
                            if family.prefix
                            else "expected_key": expected
                        },
                    )
                category, prefix = family.category, family.prefix
                saved_ref = owner if isinstance(owner, dict) else owner.ref
                if family.service in {
                    "avatar_storage",
                    "bug_attachment_storage",
                    "audit_archive_storage",
                    "export_storage",
                    "export_media_storage",
                    "prediction_inputs",
                    "image_pyramid_storage",
                    "video_derivative_storage",
                } and (
                    saved_ref["endpoint_url"] != endpoint()
                    or saved_ref["storage_identity"] != identity
                ):
                    continue
            owner_bucket = owner["bucket"] if isinstance(owner, dict) else owner.bucket
            if owner_bucket != bucket:
                continue
            for key in keys:
                if _contains(owner, key, prefix):
                    claims[key].add(category)
        except (
            ContentError,
            TrainingDatasetError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
        ):
            # Bad or legacy evidence leaves the listed object's bytes unclassified.
            continue
    for row in objects:
        categories = claims[row["key"]]
        category = next(iter(categories)) if len(categories) == 1 else "unclassified"
        result["categories"][category]["object_count"] += 1
        result["categories"][category]["size_bytes"] += row["size"]
        result["conflict_object_count"] += int(len(categories) > 1)
    return result


def valid_totals(value):
    if (
        not isinstance(value, dict)
        or type(value.get("schema")) is not int
        or value["schema"] != SCHEMA
    ):
        return False
    categories = value.get("categories")
    if not isinstance(categories, dict) or set(categories) != set(CATEGORIES):
        return False
    for item in categories.values():
        if not isinstance(item, dict) or set(item) != {"object_count", "size_bytes"}:
            return False
        if any(type(number) is not int or number < 0 for number in item.values()):
            return False
    conflicts = value.get("conflict_object_count")
    return (
        type(conflicts) is int
        and 0 <= conflicts <= categories["unclassified"]["object_count"]
    )


def merge(previous, page):
    if not valid_totals(previous) or not valid_totals(page):
        return None
    total = empty()
    for name in CATEGORIES:
        for field in ("object_count", "size_bytes"):
            total["categories"][name][field] = (
                previous["categories"][name][field] + page["categories"][name][field]
            )
    total["conflict_object_count"] = (
        previous["conflict_object_count"] + page["conflict_object_count"]
    )
    return total


def stamp(value, count, size, measured_at):
    if not valid_totals(value):
        return None
    if (
        sum(item["object_count"] for item in value["categories"].values()) != count
        or sum(item["size_bytes"] for item in value["categories"].values()) != size
    ):
        return None
    return {**value, "measured_at": measured_at.isoformat()}


def summarize(observations, bucket_count):
    """Read saved successful observations; old/mismatched evidence stays unknown."""
    total, measured, stale = empty(), [], False
    for row, status in observations:
        value = row.category_totals
        if (
            not row.measured_at
            or stamp(value, row.object_count, row.total_size_bytes, row.measured_at)
            is None
        ):
            continue
        try:
            if datetime.fromisoformat(value["measured_at"]) != row.measured_at:
                continue
        except (KeyError, ValueError, TypeError):
            continue
        total = merge(total, value)
        measured.append(row)
        stale |= status == "stale"
    count = sum(item["object_count"] for item in total["categories"].values())
    size = sum(item["size_bytes"] for item in total["categories"].values())
    return {
        "categories": [
            {"category": name, **total["categories"][name]} for name in CATEGORIES
        ],
        "sampled_size_bytes": size,
        "sampled_object_count": count,
        "total_size_bytes": size if len(measured) == bucket_count else None,
        "conflict_object_count": total["conflict_object_count"],
        "measured_bucket_count": len(measured),
        "bucket_count": bucket_count,
        "measured_from": min(
            (row.measurement_started_at or row.measured_at for row in measured),
            default=None,
        ),
        "measured_through": max((row.measured_at for row in measured), default=None),
        "has_stale_data": stale,
        "coverage": "none"
        if not measured
        else "partial"
        if len(measured) < bucket_count
        or total["categories"]["unclassified"]["object_count"]
        else "complete",
    }
