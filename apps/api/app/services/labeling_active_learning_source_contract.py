"""Session-free Labeling read and local edit-fence contracts for candidate sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypedDict
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CandidatePageRead:
    """Read the current page; implementation preserves separate count and page SQL."""

    project_id: UUID
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class CandidateSourcesRead:
    """Read requested IDs in Task.id order; empty IDs return an empty list.

    Missing, foreign, ineligible, or duplicate requested IDs raise ``pool_stale``.
    This scope is not an authorization grant; callers verify current project access.
    """

    project_id: UUID
    task_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class CandidateEditFence:
    """Fence task edits in sorted order using the currently verified actor ID."""

    task_ids: tuple[UUID, ...]
    actor_id: UUID


class CandidatePoolItem(TypedDict):
    task_id: str
    display_id: str
    file_name: str
    dataset_item_id: str | None
    scene_id: str | None


class CandidatePoolPage(TypedDict):
    total: int
    limit: int
    offset: int
    items: list[CandidatePoolItem]


class CandidateTaskState(TypedDict):
    display_id: str
    file_name: str
    file_path: str
    file_type: str
    status: str
    version: int
    is_labeled: bool
    total_annotations: int
    assignee_id: str | None
    reviewer_id: str | None
    assignee_is_override: bool
    reviewer_is_override: bool
    assigned_at: str | None
    submitted_at: str | None
    reviewed_at: str | None
    skipped_at: str | None
    skip_reason: str | None
    review_round_id: str | None
    first_reviewed_at: str | None
    first_review_result: str | None
    reopened_count: int
    tags: list[object] | None


class CandidateItemState(TypedDict):
    id: str
    dataset_id: str
    file_path: str
    file_type: str
    file_size: int | None
    content_hash: str | None
    width: int | None
    height: int | None
    scene_id: str | None
    frame_index: int | None


class CandidateBatchState(TypedDict):
    id: str
    display_id: str
    status: str
    admin_locked: bool
    annotator_id: str | None
    reviewer_id: str | None
    assigned_user_ids: list[object] | None


class CandidateSourceSnapshot(TypedDict):
    """Detached mutable facts for one read, not frozen truth or a source access grant.

    ``source_bucket`` and ``source_key`` identify data only; they grant no access.
    Mutable JSON fields remain value copies so a caller cannot mutate ORM state.
    """

    task_id: str
    project_id: str
    dataset_item_id: str | None
    scene_id: str | None
    source_bucket: str
    source_key: str
    file_type: str
    expected_width: int | None
    expected_height: int | None
    task: CandidateTaskState
    item: CandidateItemState | None
    batch: CandidateBatchState | None


class CandidateSourceError(RuntimeError):
    """Expected source or edit-fence rejection, translated by Learning."""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class LabelingActiveLearningSources(Protocol):
    async def list_candidates(self, request: CandidatePageRead) -> CandidatePoolPage:
        """Return the current ordered eligible page and its separately read count."""
        ...

    async def read_candidate_sources(
        self, request: CandidateSourcesRead
    ) -> list[CandidateSourceSnapshot]:
        """Return the selected project-scoped facts in Task.id order or raise stale."""
        ...

    async def lock_candidate_edits(self, request: CandidateEditFence) -> None:
        """Acquire current task edit fences in sorted order in the caller's transaction."""
        ...
