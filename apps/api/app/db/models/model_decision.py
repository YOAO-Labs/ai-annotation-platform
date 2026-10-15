"""Append-only candidate decisions and indexes for bounded provenance reads."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_run import TrainingRun, TrainingAttempt
from app.db.models.trained_model import (
    TrainedModelVersion,
    ModelDeployment,
    ModelRelease,
)
from app.db.models.training_recipe import TrainingRecipeRevision
from app.db.models.sample_evaluation import SampleEvaluationRun
from app.db.models.prediction import PredictionMeta


class ModelDecision(Base):
    __tablename__ = "model_decisions"
    __table_args__ = (
        UniqueConstraint(
            "model_version_id", "sequence", name="uq_model_decision_sequence"
        ),
        UniqueConstraint(
            "model_version_id", "request_id", name="uq_model_decision_request"
        ),
        CheckConstraint("sequence > 0", name="ck_model_decision_sequence"),
        CheckConstraint(
            "disposition IN ('active','research','declined','archived')",
            name="ck_model_decision_disposition",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.id", ondelete="RESTRICT"), index=True
    )
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trained_model_versions.id", ondelete="RESTRICT")
    )
    creator_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    creator_label: Mapped[str] = mapped_column(String(100))
    sequence: Mapped[int] = mapped_column(Integer)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    request_digest: Mapped[str] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(20))
    note: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


LINEAGE_INDEXES = [
    (TrainingDatasetVersion, "parent_id"),
    (TrainingRun, "dataset_id"),
    (TrainingAttempt, "resume_from_id"),
    (TrainingAttempt, "run_id"),
    (TrainedModelVersion, "run_id"),
    (TrainedModelVersion, "attempt_id"),
    (ModelDeployment, "model_version_id"),
    (ModelRelease, "model_version_id"),
    (ModelRelease, "deployment_id"),
    (ModelRelease, "previous_release_id"),
    (ModelRelease, "rollback_from_id"),
    (TrainingRecipeRevision, "recipe_id"),
    (SampleEvaluationRun, "dataset_id"),
    (SampleEvaluationRun, "model_version_id"),
]
for model, column in LINEAGE_INDEXES:
    Index(
        f"ix_lineage_{model.__tablename__}_{column}",
        getattr(model, column),
        model.created_at,
        model.id,
    )
Index(
    "ix_lineage_prediction_model",
    text("(extra -> 'trained_model' ->> 'model_version_id')"),
    PredictionMeta.id,
)
