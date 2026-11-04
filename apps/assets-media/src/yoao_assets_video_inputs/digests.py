"""Portable, bounded hash chains for video admission and sealed outcomes."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .authority import VideoSelection
from .execution import (
    CapturedVideoMember,
    EXECUTION_SCHEMA,
    VideoManifestHeader,
    VideoManifestMember,
    VideoManifestScope,
    _member_scope,
)
from .values import SAFE_INTEGER


def _fields(values) -> bytes:
    result = bytearray()
    for value in values:
        if value is None:
            result.extend(b"N;")
        else:
            raw = str(int(value) if type(value) is bool else value).encode("utf-8")
            result.extend(f"S{len(raw)}:".encode("ascii"))
            result.extend(raw)
            result.extend(b";")
    return bytes(result)


def _hash(domain: str, values) -> str:
    return hashlib.sha256(domain.encode("ascii") + b"\n" + _fields(values)).hexdigest()


def selection_seed(
    scope: VideoManifestScope,
    selection: VideoSelection,
    candidate_count: int,
    selected_count: int,
) -> str:
    if not (
        type(candidate_count) is int
        and type(selected_count) is int
        and 0 <= selected_count <= candidate_count <= SAFE_INTEGER
        and (selection.task_ids is None or candidate_count == len(selection.task_ids))
        and (
            selection.predict_mode == "skip_predicted"
            or selected_count == candidate_count
        )
        and (selection.execution_scope != "workbench" or selection.batch_id is None)
    ):
        raise ValueError("video capture changed its selector counts")
    return _hash(
        "selection",
        (
            EXECUTION_SCHEMA,
            scope.job_id,
            scope.actor_id,
            scope.project_id,
            selection.batch_id,
            selection.execution_scope,
            selection.predict_mode,
            selection.sampling.max_frames,
            selection.sampling.step,
            selection.sampling.width,
            selection.sampling.format,
            None if selection.task_ids is None else len(selection.task_ids),
            *(selection.task_ids or ()),
            candidate_count,
            selected_count,
        ),
    )


def member_digest(previous: str, member: CapturedVideoMember) -> str:
    task, source = member.task, member.source
    identity = source.object_identity if source is not None else None
    return _hash(
        "member",
        (
            previous,
            member.ordinal,
            task.task_id,
            task.project_id,
            task.batch_id,
            task.task_version,
            task.task_source_generation,
            task.dataset_item_id,
            task.task_source_snapshot,
            task.file_path,
            task.file_type,
            source.dataset_item_id if source else None,
            source.dataset_id if source else None,
            source.source_generation if source else None,
            source.source_snapshot if source else None,
            source.timetable_revision if source else None,
            identity.etag if identity else None,
            identity.byte_size if identity else None,
            identity.version_id if identity else None,
            source.source_drifted if source else None,
            source.file_path if source else None,
            source.file_type if source else None,
            source.file_size if source else None,
            source.content_hash if source else None,
            member.error,
        ),
    )


def selection_digest(
    scope: VideoManifestScope,
    selection: VideoSelection,
    candidate_count: int,
    selected_count: int,
    members: Iterable[CapturedVideoMember],
) -> str:
    digest = selection_seed(scope, selection, candidate_count, selected_count)
    count, previous_task = 0, None
    for count, member in enumerate(members, start=1):
        task = member.task
        if (
            count > selected_count
            or member.ordinal != count
            or task.project_id != scope.project_id
            or (previous_task is not None and str(task.task_id) <= previous_task)
            or (
                selection.task_ids is not None
                and task.task_id not in selection.task_ids
            )
            or (selection.batch_id is not None and task.batch_id != selection.batch_id)
        ):
            raise ValueError("video capture changed complete ordered membership")
        digest, previous_task = member_digest(digest, member), str(task.task_id)
    if count != selected_count:
        raise ValueError("video capture omitted selected members")
    return digest


def sealed_seed(
    header: VideoManifestHeader, ready_count: int, failed_count: int
) -> str:
    if (
        type(ready_count) is not int
        or type(failed_count) is not int
        or min(ready_count, failed_count) < 0
        or ready_count + failed_count != header.selected_count
        or header.prepared_count != header.selected_count
    ):
        raise ValueError("video seal omitted member outcomes")
    return _hash(
        "sealed",
        (
            EXECUTION_SCHEMA,
            header.job_id,
            header.actor_id,
            header.project_id,
            header.selection_digest,
            header.selected_count,
            ready_count,
            failed_count,
        ),
    )


def outcome_digest(previous: str, member: VideoManifestMember) -> str:
    if member.state not in {"ready", "failed"}:
        raise ValueError("video seal contains unfinished preparation")
    record = member.input
    plan = record.plan if record is not None and member.state == "ready" else None
    return _hash(
        "outcome",
        (
            previous,
            member.ordinal,
            member.task.task_id,
            member.state,
            member.error,
            record.input_id if record else None,
            plan.plan_digest if plan else None,
            plan.timebase_digest if plan else None,
            plan.input.blob.sha256 if plan else None,
            plan.input.blob.byte_size if plan else None,
            plan.selected_count if plan else None,
        ),
    )


def sealed_digest(
    header: VideoManifestHeader,
    ready_count: int,
    failed_count: int,
    members: Iterable[VideoManifestMember],
) -> str:
    digest = sealed_seed(header, ready_count, failed_count)
    count, ready, failed, previous_task = 0, 0, 0, None
    for count, member in enumerate(members, start=1):
        _member_scope(header, member)
        if (
            member.ordinal != count
            or count > header.selected_count
            or (previous_task is not None and str(member.task.task_id) <= previous_task)
        ):
            raise ValueError("video seal changed ordered membership")
        digest, previous_task = outcome_digest(digest, member), str(member.task.task_id)
        ready += member.state == "ready"
        failed += member.state == "failed"
    if (count, ready, failed) != (header.selected_count, ready_count, failed_count):
        raise ValueError("video seal changed complete outcomes")
    return digest
