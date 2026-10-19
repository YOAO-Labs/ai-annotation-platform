from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal


LabelingWriteOperation = Literal[
    "candidate_accept",
    "candidate_reject",
    "interactive_frame_prediction",
    "interactive_mask_accept",
    "mask_repair",
    "preannotation",
    "prediction_import",
    "prediction_retry",
    "secondary_inference",
    "test_seed",
]

TaskSourceBinding = tuple[
    uuid.UUID,
    int,
    uuid.UUID | None,
    str | None,
    str | None,
]


def capture_task_source_binding(task) -> TaskSourceBinding:
    """Freeze Task source facts before I/O can refresh its ORM identity-map row."""
    return (
        task.project_id,
        int(task.media_source_generation or 1),
        task.dataset_item_id,
        task.file_path,
        task.file_type,
    )


def task_source_binding_matches(task, binding: TaskSourceBinding) -> bool:
    """Compare current locked Task source facts with their preflight snapshot."""
    return capture_task_source_binding(task) == binding


@dataclass(frozen=True, slots=True)
class LabelingWriteScope:
    """Immutable actor and target snapshot carried to labeling write commands.

    ``actor_known`` distinguishes a known actorless execution from an unavailable
    actor identity; a scope object alone never authorizes a remote write.
    """

    task_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_known: bool
    operation: LabelingWriteOperation
    execution_scope: str | None
    execution_scope_known: bool
    task_version: int | None
    task_status: str
    assignee_id: uuid.UUID | None
    review_round_id: uuid.UUID | None
    source_annotation_id: uuid.UUID | None
    source_annotation_version: int | None
    expected_batch_id: uuid.UUID | None
    expected_batch_id_known: bool
    job_id: uuid.UUID | None
    job_id_known: bool
    # Older serialized scopes stay unknown rather than borrowing current facts.
    source_binding_known: bool = False
    task_source_generation: int | None = None
    source_dataset_item_id: uuid.UUID | None = None
    source_file_path: str | None = None
    source_file_type: str | None = None
    accepted_input_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class LabelingBatchWriteScope:
    """Immutable actor and target snapshot for a labeling-owned batch command."""

    batch_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_known: bool
    operation: LabelingWriteOperation
    execution_scope: str | None
    execution_scope_known: bool
    job_id: uuid.UUID | None
    job_id_known: bool


def capture_labeling_write_scope(
    task,
    *,
    actor_id: uuid.UUID | None,
    actor_known: bool,
    operation: LabelingWriteOperation,
    execution_scope: str | None,
    execution_scope_known: bool,
    job_id: uuid.UUID | None,
    job_id_known: bool,
    source_annotation=None,
    accepted_input_id: uuid.UUID | None = None,
) -> LabelingWriteScope:
    """Capture the task facts available before potentially long inference work."""
    return LabelingWriteScope(
        task_id=task.id,
        project_id=task.project_id,
        actor_id=actor_id,
        actor_known=actor_known,
        operation=operation,
        execution_scope=execution_scope,
        execution_scope_known=execution_scope_known,
        task_version=task.version,
        task_status=task.status,
        assignee_id=task.assignee_id,
        review_round_id=task.review_round_id,
        source_annotation_id=(
            source_annotation.id if source_annotation is not None else None
        ),
        source_annotation_version=(
            source_annotation.version if source_annotation is not None else None
        ),
        expected_batch_id=task.batch_id,
        expected_batch_id_known=True,
        job_id=job_id,
        job_id_known=job_id_known,
        source_binding_known=all(
            hasattr(task, field)
            for field in (
                "media_source_generation",
                "dataset_item_id",
                "file_path",
                "file_type",
            )
        ),
        task_source_generation=getattr(task, "media_source_generation", None),
        source_dataset_item_id=getattr(task, "dataset_item_id", None),
        source_file_path=getattr(task, "file_path", None),
        source_file_type=getattr(task, "file_type", None),
        accepted_input_id=accepted_input_id,
    )
