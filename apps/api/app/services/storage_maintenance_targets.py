"""Saved target responsibilities, separate from source lineage and live bindings."""

from sqlalchemy import Text, case, cast, func, literal, select, union, union_all
from sqlalchemy.dialects.postgresql import UUID, aggregate_order_by

from app.db.models.active_learning import ActiveLearningRound
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.avatar_upload import AvatarUpload
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.comment_attachment_upload import CommentAttachmentUpload
from app.db.models.dataset import DatasetItem, ProjectDataset
from app.db.models.dataset_asset import (
    DatasetAsset,
    DatasetAssetLocation,
    StorageBinding,
)
from app.db.models.dataset_auxiliary_original import DatasetAuxiliaryOriginal
from app.db.models.dataset_source import DatasetSource, DatasetSourcePreview
from app.db.models.export_media_bundle import ExportMediaBundle
from app.db.models.export_output import ExportOutput
from app.db.models.guide_asset_upload import GuideAssetUpload
from app.db.models.image_pyramid import ImagePyramidAsset, ImagePyramidOutput
from app.db.models.managed_original_creation import ManagedOriginalCreation
from app.db.models.managed_original_retirement import ManagedOriginalRetirement
from app.db.models.mask_format_import import MaskFormatImport
from app.db.models.mask_format_upload import MaskFormatUpload
from app.db.models.original_cache import OriginalCacheEntry
from app.db.models.prediction_input import PredictionInputLease
from app.db.models.project_guide_asset import ProjectGuideAsset
from app.db.models.raster_mask_object import RasterMaskObject
from app.db.models.sample_evaluation import SampleEvaluationRun
from app.db.models.task import Task
from app.db.models.task_original import TaskOriginal
from app.db.models.thumbnail_output import ThumbnailOutput
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_preview import TrainingPreview
from app.db.models.training_run import TrainingRun
from app.db.models.training_storage_deletion import TrainingStorageDeletion
from app.db.models.video_derivative import VideoDerivativeOutput


PURPOSES = (
    "annotations",
    "datasets",
    "training",
    "media-cache",
    "audit-archive",
    "import",
    "export",
    "avatars",
    "bug-reports",
)


def confirmation_counts(counts):
    """Global unknown history is coverage, never a selected-location dependency."""
    return {
        key: int(value) for key, value in counts.items() if key != "unlocated_targets"
    }


def _digest(value):
    return func.encode(func.sha256(func.convert_to(value, "UTF8")), "hex")


def _fingerprint(statement):
    rows = statement.subquery()
    value = rows.c.fingerprint
    return (
        select(
            _digest(
                func.coalesce(
                    func.string_agg(value, aggregate_order_by(literal(""), value)), ""
                )
            )
        )
        .select_from(rows)
        .scalar_subquery()
    )


def _roles(*, with_fingerprint):
    """Project only saved locators and semantic identities from the 27 owners.

    A Creation has two roles when staging exists. No current binding, object key
    parsing or private business document can establish a missing saved location.
    """
    statements = []
    associations = (
        "dataset_id",
        "project_id",
        "task_id",
        "item_id",
        "source_id",
        "asset_id",
    )

    def role(
        model,
        purpose,
        fields,
        *,
        location=None,
        ref="storage_ref",
        kind="output",
        identity=None,
        where=(),
        joins=(),
        locator_fields=(),
        **links,
    ):
        locator = getattr(model, ref) if ref else None
        if location is None:
            location = locator["location_id"].astext
        values = [getattr(model, name) for name in fields.split()] + list(
            locator_fields
        )
        # This stays inside SQL; neither references nor business documents are
        # returned by the aggregate. Poll/access/retry timestamps are omitted.
        fingerprint = (
            _digest(
                cast(func.jsonb_build_array(model.__tablename__, kind, *values), Text)
            )
            if with_fingerprint
            else literal("")
        )
        statement = select(
            literal(model.__tablename__).label("family"),
            literal(kind).label("role"),
            (literal(purpose) if isinstance(purpose, str) else purpose).label(
                "purpose"
            ),
            cast(identity if identity is not None else model.id, Text).label(
                "record_id"
            ),
            cast(location, Text).label("location_id"),
            *[
                links.get(name, literal(None, type_=UUID)).label(name)
                for name in associations
            ],
            fingerprint.label("fingerprint"),
        ).select_from(model)
        for target, condition in joins:
            statement = statement.outerjoin(target, condition)
        statements.append(statement.where(*where))

    role(
        PredictionInputLease,
        "import",
        "id owner_kind owner_id task_id contract_version storage_ref bucket storage_identity state",
        task_id=PredictionInputLease.task_id,
    )
    role(
        ImagePyramidOutput,
        "media-cache",
        "id asset_id generation lease_token storage_ref state",
        joins=(
            (ImagePyramidAsset, ImagePyramidAsset.id == ImagePyramidOutput.asset_id),
        ),
        item_id=ImagePyramidAsset.dataset_item_id,
        task_id=ImagePyramidAsset.task_id,
    )
    role(
        VideoDerivativeOutput,
        "media-cache",
        "id dataset_item_id kind metadata_output_id storage_ref state",
        item_id=VideoDerivativeOutput.dataset_item_id,
    )
    role(
        ExportMediaBundle,
        "export",
        "id project_id job_id zip_output_id storage_ref state",
        project_id=ExportMediaBundle.project_id,
    )
    role(
        TrainingDatasetVersion,
        "training",
        "id project_id parent_id attempt_id status storage_ref snapshot_digest",
        project_id=TrainingDatasetVersion.project_id,
    )
    role(
        TrainingRun,
        "training",
        "id project_id dataset_id current_attempt_id status storage_ref",
        project_id=TrainingRun.project_id,
    )
    role(
        TrainingPreview,
        "training",
        "id project_id dataset_id claim_id status storage_ref",
        project_id=TrainingPreview.project_id,
    )
    role(
        SampleEvaluationRun,
        "training",
        "id project_id dataset_id generation current_attempt_id status storage_ref",
        project_id=SampleEvaluationRun.project_id,
    )
    role(
        ActiveLearningRound,
        "training",
        "id project_id generation current_attempt_id status storage_ref",
        project_id=ActiveLearningRound.project_id,
    )
    role(
        AvatarUpload,
        "avatars",
        "token owner_id state storage_ref object_version_id",
        identity=AvatarUpload.token,
    )
    role(
        BugAttachmentUpload,
        "bug-reports",
        "id owner_id bound_report_id state storage_ref object_version_id",
    )
    role(
        CommentAttachmentUpload,
        "annotations",
        "id owner_id project_id task_id annotation_id bound_comment_id state storage_ref",
        project_id=CommentAttachmentUpload.project_id,
        task_id=CommentAttachmentUpload.task_id,
    )
    role(
        GuideAssetUpload,
        "annotations",
        "id owner_id project_id state storage_ref",
        project_id=GuideAssetUpload.project_id,
    )
    role(
        RasterMaskObject,
        "annotations",
        "id logical_key canonical_sha256 stored_sha256 stored_size_bytes storage_encoding state storage_ref",
    )
    role(
        ExportOutput,
        "export",
        "id project_id job_id media_bundle_id state storage_ref",
        project_id=ExportOutput.project_id,
    )
    role(AuditArchiveOutput, "audit-archive", "id job_id year month state storage_ref")
    creation = ManagedOriginalCreation
    final_purpose = case(
        (creation.owner_kind == "task", "annotations"), else_="datasets"
    )
    role(
        creation,
        final_purpose,
        "id owner_kind dataset_id project_id task_id status location_id bucket object_key storage_identity version_id",
        location=creation.location_id,
        ref=None,
        kind="final",
        locator_fields=(
            creation.configuration["original_ref"],
            creation.configuration["endpoint"],
        ),
        dataset_id=creation.dataset_id,
        project_id=creation.project_id,
        task_id=creation.task_id,
    )
    role(
        creation,
        "import",
        "id owner_kind dataset_id project_id task_id status staging_ref staging_bucket staging_key origin",
        ref="staging_ref",
        kind="staging",
        where=(creation.staging_key.is_not(None), creation.staging_key != ""),
        dataset_id=creation.dataset_id,
        project_id=creation.project_id,
        task_id=creation.task_id,
    )
    retirement = ManagedOriginalRetirement
    role(
        retirement,
        "datasets",
        "id assertion_digest creation_id dataset_id dataset_item_id asset_id location_id bucket object_key storage_identity location_config_revision binding_generation role identity_state reference_state",
        location=retirement.location_id,
        ref=None,
        dataset_id=retirement.dataset_id,
        item_id=retirement.dataset_item_id,
        asset_id=retirement.asset_id,
    )
    asset_location = DatasetAssetLocation
    role(
        asset_location,
        "datasets",
        "asset_id location_id bucket object_key role reference_state creation_id storage_ref",
        location=asset_location.location_id,
        identity=func.jsonb_build_array(
            asset_location.asset_id, asset_location.location_id, asset_location.bucket
        ),
        where=(asset_location.role == "managed_original",),
        asset_id=asset_location.asset_id,
    )
    role(
        TaskOriginal,
        "annotations",
        "id creation_id task_id project_id location_id bucket object_key storage_identity storage_ref version_id",
        location=TaskOriginal.location_id,
        task_id=TaskOriginal.task_id,
        project_id=TaskOriginal.project_id,
    )
    role(
        DatasetAuxiliaryOriginal,
        "datasets",
        "id creation_id dataset_id kind location_id bucket object_key storage_identity storage_ref version_id",
        location=DatasetAuxiliaryOriginal.location_id,
        dataset_id=DatasetAuxiliaryOriginal.dataset_id,
    )
    preview = DatasetSourcePreview
    role(
        preview,
        "media-cache",
        "source_id path_digest asset_id thumbnail_output_id generation policy_revision state location_id bucket object_key",
        location=preview.location_id,
        ref=None,
        identity=func.jsonb_build_array(preview.source_id, preview.path_digest),
        where=(
            preview.bucket.is_not(None),
            preview.object_key.is_not(None),
            preview.bucket != "",
            preview.object_key != "",
        ),
        source_id=preview.source_id,
        asset_id=preview.asset_id,
    )
    cache = OriginalCacheEntry
    role(
        cache,
        "media-cache",
        "id source_id asset_id sha256 scope_location_id source_revision policy_revision location_revision location_id bucket binding_generation storage_identity storage_ref state",
        location=cache.location_id,
        source_id=cache.source_id,
        asset_id=cache.asset_id,
    )
    role(TrainingStorageDeletion, "training", "id storage_ref bucket prefix")
    role(
        MaskFormatUpload,
        "import",
        "id project_id uploader_id logical_object_key storage_ref",
        project_id=MaskFormatUpload.project_id,
    )
    role(
        MaskFormatImport,
        "import",
        "id project_id staged_object_key staged_sha256 staging_ref status",
        ref="staging_ref",
        project_id=MaskFormatImport.project_id,
    )
    thumb = ThumbnailOutput
    role(
        thumb,
        "media-cache",
        "id owner_kind owner_id path_digest state storage_ref version_id",
        source_id=case((thumb.owner_kind == "directory_preview", thumb.owner_id)),
        item_id=case((thumb.owner_kind == "dataset_thumbnail", thumb.owner_id)),
        task_id=case((thumb.owner_kind == "task_thumbnail", thumb.owner_id)),
    )
    return union_all(*statements).cte("maintenance_saved_roles")


def dependencies(location_id, *, with_fingerprint=False):
    """Return scalar count columns and a fixed-size, location-only SQL proof."""
    roles = _roles(with_fingerprint=with_fingerprint)
    selected = (
        select(roles)
        .where(roles.c.location_id == str(location_id))
        .cte("maintenance_targets")
    )

    def ids(column):
        return select(column).where(column.is_not(None))

    def count(statement):
        return select(func.count()).select_from(statement.subquery()).scalar_subquery()

    items = union(
        ids(selected.c.item_id),
        select(DatasetAsset.dataset_item_id).where(
            DatasetAsset.id.in_(ids(selected.c.asset_id))
        ),
        select(Task.dataset_item_id).where(
            Task.id.in_(ids(selected.c.task_id)), Task.dataset_item_id.is_not(None)
        ),
    ).cte("maintenance_target_items")
    tasks = union(
        ids(selected.c.task_id),
        select(Task.id).where(Task.dataset_item_id.in_(select(items))),
    ).cte("maintenance_target_tasks")
    datasets = union(
        ids(selected.c.dataset_id),
        select(DatasetSource.dataset_id).where(
            DatasetSource.id.in_(ids(selected.c.source_id)),
            DatasetSource.dataset_id.is_not(None),
        ),
        select(DatasetItem.dataset_id).where(DatasetItem.id.in_(select(items))),
    ).cte("maintenance_target_datasets")
    guides = select(ProjectGuideAsset).where(
        cast(ProjectGuideAsset.upload_id, Text).in_(
            select(selected.c.record_id).where(
                selected.c.family == GuideAssetUpload.__tablename__
            )
        )
    )
    projects = union(
        ids(selected.c.project_id),
        select(Task.project_id).where(Task.id.in_(select(tasks))),
        select(ProjectDataset.project_id).where(
            ProjectDataset.dataset_id.in_(select(datasets))
        ),
        guides.with_only_columns(ProjectGuideAsset.project_id),
    )
    bindings = select(StorageBinding).where(StorageBinding.location_id == location_id)
    counts = {
        "target_records": count(select(selected)),
        **{
            "target_" + purpose.replace("-", "_"): count(
                select(selected.c.record_id).where(selected.c.purpose == purpose)
            )
            for purpose in PURPOSES
        },
        "target_related_datasets": count(select(datasets)),
        "target_related_projects": count(projects),
        "target_related_tasks": count(select(tasks)),
        "target_versions": count(
            select(selected.c.record_id).where(
                selected.c.family == TrainingDatasetVersion.__tablename__
            )
        ),
        "target_runs": count(
            select(selected.c.record_id).where(
                selected.c.family == TrainingRun.__tablename__
            )
        ),
        "current_bindings": count(bindings),
        "unlocated_targets": count(
            select(roles.c.record_id).where(roles.c.location_id.is_(None))
        ),
    }
    if not with_fingerprint:
        return counts, None

    def footprint(model, fields, *where):
        return _fingerprint(
            select(
                _digest(
                    cast(
                        func.jsonb_build_array(
                            model.__tablename__,
                            *[getattr(model, field) for field in fields.split()],
                        ),
                        Text,
                    )
                ).label("fingerprint")
            )
            .select_from(model)
            .where(*where)
        )

    fingerprints = [
        _fingerprint(select(selected.c.fingerprint)),
        footprint(
            StorageBinding,
            "purpose location_id bucket generation",
            StorageBinding.location_id == location_id,
        ),
        footprint(
            DatasetAsset,
            "id dataset_item_id",
            DatasetAsset.id.in_(ids(selected.c.asset_id)),
        ),
        footprint(
            DatasetSource,
            "id dataset_id",
            DatasetSource.id.in_(ids(selected.c.source_id)),
        ),
        footprint(DatasetItem, "id dataset_id", DatasetItem.id.in_(select(items))),
        footprint(Task, "id dataset_item_id project_id", Task.id.in_(select(tasks))),
        footprint(
            ProjectDataset,
            "id dataset_id project_id",
            ProjectDataset.dataset_id.in_(select(datasets)),
        ),
        footprint(
            ProjectGuideAsset,
            "project_id upload_id",
            ProjectGuideAsset.upload_id.in_(
                guides.with_only_columns(ProjectGuideAsset.upload_id)
            ),
        ),
        footprint(
            ImagePyramidAsset,
            "id dataset_item_id task_id",
            ImagePyramidAsset.id.in_(
                select(ImagePyramidOutput.asset_id).where(
                    ImagePyramidOutput.storage_ref["location_id"].astext
                    == str(location_id)
                )
            ),
        ),
    ]
    return counts, _digest(cast(func.jsonb_build_array(*fingerprints), Text))
