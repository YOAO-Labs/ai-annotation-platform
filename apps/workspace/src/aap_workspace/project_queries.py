"""Portable Project query policy and its ordered, read-only data phases."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
import json
from typing import AsyncContextManager, Protocol
import uuid

from aap_workspace_contract import derive_classes_list

from aap_workspace_contract.access import (
    MANAGER_PLATFORM_ROLES,
    PROJECT_ROLES,
)
from aap_workspace_contract.project_query_contract import (
    PROJECT_QUERY_PROTOCOL,
    ProjectClassRenameEvidence,
    ProjectQueryFailure,
    ProjectQueryRejected,
    ProjectQueryProjectionMissing,
    ProjectQuerySuccess,
    ProjectQueryUnavailable,
    decode_project_query_filters,
    decode_project_query_request,
    decode_project_query_step,
)
from aap_workspace_contract.project_query_values import (
    decode_project_query_reply,
    decode_project_row,
    decode_project_rows,
    project_access_data,
    project_json_value,
)
from aap_workspace_contract.project_read_authority import (
    accessible_project_row,
    current_project_access,
)


class ProjectOwnerBrief(Protocol):
    id: uuid.UUID
    name: str
    email: str
    role: str | None
    avatar_initial: str
    avatar_ref: str | None


class ProjectQueryProfiles(Protocol):
    async def resolve(
        self, user_ids: list[uuid.UUID]
    ) -> Mapping[str, ProjectOwnerBrief]: ...


class ProjectQueryProfilesUnavailable(Exception):
    """Identity Briefs failed after the query reached its profile phase."""


class ProjectQueryTransaction(Protocol):
    async def step(self, request, action, parameters=None, filters=None): ...


class ProjectQueryData(Protocol):
    def transaction(self) -> AsyncContextManager[ProjectQueryTransaction]: ...


def _date_filter(value, *, end_of_day=False):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ProjectQueryRejected(422, "创建日期须为 ISO 日期或时间") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if end_of_day and len(value) == 10:
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def _list_filters(request):
    filters = decode_project_query_filters(request)
    if request.operation == "search_projects":
        return filters.model_dump(mode="python")
    if filters.project_role and filters.project_role not in PROJECT_ROLES:
        raise ProjectQueryRejected(422, "非法项目职责")
    start = _date_filter(filters.created_from)
    end = _date_filter(filters.created_to, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise ProjectQueryRejected(422, "创建开始日期不能晚于结束日期")
    return {
        **filters.model_dump(mode="python"),
        "created_from": start,
        "created_to": end,
    }


def _date_bounds(filters):
    result = {}
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for key in ("created_from", "created_to"):
        value = filters[key]
        delta = value - epoch if value is not None else None
        result[key + "_us"] = (
            str(
                delta.days * 86_400_000_000
                + delta.seconds * 1_000_000
                + delta.microseconds
            )
            if delta is not None
            else None
        )
    return result


class ProjectQueryManager:
    def __init__(
        self,
        data: ProjectQueryData,
        profiles: ProjectQueryProfiles,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.data = data
        self.profiles = profiles
        self.clock = clock or (lambda: datetime.now(UTC))

    async def execute(self, request):
        request = decode_project_query_request(request)
        try:
            filters = (
                _list_filters(request) if request.filters_ascii is not None else None
            )
            evidence = request.projection_evidence
            if isinstance(evidence, ProjectClassRenameEvidence) and not (
                evidence.is_active
                and (
                    evidence.platform_role == "super_admin"
                    or (
                        evidence.platform_role in MANAGER_PLATFORM_ROLES
                        and evidence.owner_id == evidence.actor_id
                    )
                )
            ):
                raise ProjectQueryUnavailable()
            async with self.data.transaction() as transaction:
                if request.operation in ("list_projects", "query_projects"):
                    data = await self._list(request, transaction, filters)
                elif request.operation in ("get_project", "project_readiness"):
                    row = await accessible_project_row(request, transaction, self._step)
                    data = (
                        await self._readiness(request, transaction, row)
                        if request.operation == "project_readiness"
                        else await self._detail(request, transaction, row)
                    )
                elif request.operation in (
                    "require_project_manager",
                    "require_project_visible",
                ):
                    await accessible_project_row(request, transaction, self._step)
                    data = {"project_id": str(request.target_id)}
                elif request.operation == "get_project_access":
                    await accessible_project_row(request, transaction, self._step)
                    access = await current_project_access(
                        request, transaction, self._step
                    )
                    data = project_access_data(access)
                elif request.operation == "project_stats":
                    data = await self._stats(request, transaction)
                elif request.operation == "get_project_projection":
                    facts = await self._step(request, transaction, "projection_row")
                    row = decode_project_row(
                        facts.row_json, target_id=request.target_id
                    )
                    if row is None:
                        raise ProjectQueryProjectionMissing(
                            "Accepted Project projection row disappeared"
                        )
                    data = await self._serialize(request, transaction, row)
                elif request.operation == "search_projects":
                    facts = await self._step(
                        request, transaction, "search_rows", filters=filters
                    )
                    rows = decode_project_rows(facts.rows_json)
                    data = [
                        {
                            key: row[key]
                            for key in (
                                "id",
                                "display_id",
                                "name",
                                "type_key",
                                "data_type",
                                "type_label",
                            )
                        }
                        for row in rows
                    ]
                else:
                    raise ProjectQueryUnavailable()
        except ProjectQueryProfilesUnavailable:
            return ProjectQueryFailure(
                schema=PROJECT_QUERY_PROTOCOL,
                ok=False,
                request=request,
                error={
                    "status_code": 503,
                    "detail": "身份资料服务暂不可用",
                    "headers": None,
                },
            )
        except ProjectQueryRejected as error:
            return ProjectQueryFailure(
                schema=PROJECT_QUERY_PROTOCOL,
                ok=False,
                request=request,
                error=error.error,
            )
        reply = ProjectQuerySuccess(
            schema=PROJECT_QUERY_PROTOCOL,
            ok=True,
            request=request,
            data_json=json.dumps(
                data,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
        return decode_project_query_reply(request, reply)

    @staticmethod
    async def _step(request, transaction, action, parameters=None, *, filters=None):
        raw = await transaction.step(
            request, action, parameters=parameters, filters=filters
        )
        return decode_project_query_step(request, action, parameters, raw).facts

    async def _owner_briefs(self, rows):
        ids = {
            uuid.UUID(row["owner_id"]) for row in rows if row["owner_id"] is not None
        }
        if not ids:
            return {}
        return await self.profiles.resolve(list(ids))

    @staticmethod
    def _batch_summary(row):
        return (
            row["batch_summary"]
            if project_json_value(row, "batch_summary")
            else '{"total":0,"assigned":0,"in_review":0}'
        )

    async def _serialize(
        self,
        request,
        transaction,
        row,
        *,
        owners=None,
        ai_lookup=None,
        batch_summary=None,
    ):
        if owners is None:
            owners = await self._owner_briefs([row])
        owner = owners.get(row["owner_id"])
        member_count = await self._step(
            request, transaction, "member_count", {"project_id": row["id"]}
        )
        if ai_lookup is None:
            ai = await self._step(
                request, transaction, "ai_counts", {"project_ids": [row["id"]]}
            )
            ai_lookup = {str(item.project_id): item.count for item in ai.counts}
        if batch_summary is None:
            batch_summary = self._batch_summary(row)
        pool_id = row["ml_backend_pool_id"]
        legacy_id = None
        if pool_id is not None:
            pool = await self._step(request, transaction, "pool", {"pool_id": pool_id})
            legacy_id = (
                str(pool.legacy_instance_id)
                if pool.legacy_instance_id is not None
                else None
            )
        return {
            **row,
            "ml_backend_id": legacy_id,
            "owner_name": owner.name if owner is not None else None,
            "owner_avatar_ref": owner.avatar_ref if owner is not None else None,
            "member_count": member_count.count,
            "ai_completed_tasks": ai_lookup.get(row["id"], "0"),
            "batch_summary": batch_summary,
        }

    async def _roles(self, request, transaction, rows):
        if not rows or request.admitted_actor.role == "super_admin":
            return {}
        facts = await self._step(
            request,
            transaction,
            "my_roles",
            {"project_ids": [row["id"] for row in rows]},
        )
        return {str(item.project_id): item.role for item in facts.memberships}

    async def _list(self, request, transaction, filters):
        total = None
        bounds = _date_bounds(filters)
        if request.operation == "query_projects":
            total = (
                await self._step(
                    request, transaction, "count_rows", bounds, filters=filters
                )
            ).count
        facts = await self._step(
            request, transaction, "list_rows", bounds, filters=filters
        )
        rows = decode_project_rows(facts.rows_json)
        batch_summaries = {row["id"]: self._batch_summary(row) for row in rows}
        ai_lookup = {}
        if rows:
            ai = await self._step(
                request,
                transaction,
                "ai_counts",
                {"project_ids": [row["id"] for row in rows]},
            )
            ai_lookup = {str(item.project_id): item.count for item in ai.counts}
        owners = await self._owner_briefs(rows)
        items = [
            await self._serialize(
                request,
                transaction,
                row,
                owners=owners,
                ai_lookup=ai_lookup,
                batch_summary=batch_summaries[row["id"]],
            )
            for row in rows
        ]
        roles = await self._roles(request, transaction, rows)
        for item in items:
            item["my_project_role"] = roles.get(item["id"])
        if total is None:
            return items
        pagination = request.pagination
        return {
            "items": items,
            "total": total,
            "page": pagination.page,
            "page_size": pagination.page_size,
            "pages": str(
                (int(total) + pagination.page_size - 1) // pagination.page_size
            ),
        }

    async def _detail(self, request, transaction, row):
        data = await self._serialize(request, transaction, row)
        data["my_project_role"] = (await self._roles(request, transaction, [row])).get(
            row["id"]
        )
        return data

    async def _stats(self, request, transaction):
        facts = await self._step(request, transaction, "visible_rows")
        rows = decode_project_rows(facts.rows_json)
        total = sum(row["total_tasks"] for row in rows)
        completed = sum(row["completed_tasks"] for row in rows)
        review = sum(row["review_tasks"] for row in rows)
        data = {
            "total_data": str(total),
            "completed": str(completed),
            "pending_review": str(review),
            "ai_rate": 0.0,
            "total_annotations": "0",
            "ai_derived_annotations": "0",
            "total_data_series": ["0"] * 12,
            "completed_series": ["0"] * 12,
            "pending_review_series": ["0"] * 12,
            "ai_rate_series": [0.0] * 12,
        }
        if not rows:
            return data
        ids = [row["id"] for row in rows]
        total_ann = (
            await self._step(
                request,
                transaction,
                "annotation_count",
                {"project_ids": ids, "ai_only": False},
            )
        ).count
        ai_ann = (
            await self._step(
                request,
                transaction,
                "annotation_count",
                {"project_ids": ids, "ai_only": True},
            )
        ).count
        ai_rate = (
            round(int(ai_ann) / int(total_ann) * 100, 1) if int(total_ann) else 0.0
        )
        now = self.clock()
        ends = [
            (now - timedelta(days=7) * (11 - index)).isoformat() for index in range(12)
        ]
        parameters = {"project_ids": ids, "bucket_ends": ends}
        tasks = await self._step(request, transaction, "task_series", parameters)
        annotations = await self._step(
            request, transaction, "annotation_series", parameters
        )
        return {
            **data,
            "total_annotations": total_ann,
            "ai_derived_annotations": ai_ann,
            "ai_rate": ai_rate,
            "total_data_series": tasks.total_data,
            "completed_series": tasks.completed,
            "pending_review_series": tasks.pending_review,
            "ai_rate_series": [
                round(int(ai) / int(total) * 100, 1) if int(total) else 0.0
                for ai, total in zip(annotations.ai_derived, annotations.total)
            ],
        }

    async def _readiness(self, request, transaction, row):
        dataset_ids = (
            await self._step(request, transaction, "dataset_ids")
        ).dataset_ids
        item_count = linked_count = "0"
        if dataset_ids:
            parameters = {"dataset_ids": [str(value) for value in dataset_ids]}
            item_count = (
                await self._step(request, transaction, "dataset_item_count", parameters)
            ).count
            linked_count = (
                await self._step(request, transaction, "linked_task_count", parameters)
            ).count
        task_count = (await self._step(request, transaction, "task_count")).count
        statuses = (await self._step(request, transaction, "job_counts")).statuses
        latest = (await self._step(request, transaction, "latest_job_status")).status
        batches = (await self._step(request, transaction, "batches")).batches
        counts = (
            await self._step(request, transaction, "batch_task_counts")
        ).batch_task_counts
        members = (await self._step(request, transaction, "active_members")).members
        job_counts = {item.status: int(item.count) for item in statuses}
        task_counts = {item.batch_id: int(item.count) for item in counts}
        annotators = {
            item.user_id for item in members if item.project_role == "annotator"
        }
        reviewers = {
            item.user_id for item in members if item.project_role == "reviewer"
        }
        executable_statuses = {
            "active",
            "pre_annotated",
            "annotating",
            "reviewing",
            "rejected",
        }
        nonempty = sum(task_counts.get(batch.id, 0) > 0 for batch in batches)
        executable = sum(
            batch.status in executable_statuses and task_counts.get(batch.id, 0) > 0
            for batch in batches
        )
        assigned = sum(
            batch.status in executable_statuses
            and task_counts.get(batch.id, 0) > 0
            and batch.annotator_id in annotators
            and batch.reviewer_id in reviewers
            for batch in batches
        )
        return {
            "project_id": row["id"],
            "guide_ready": bool((row["annotation_guide"] or "").strip()),
            "classes_ready": bool(
                derive_classes_list(project_json_value(row, "tool_bindings"))
            ),
            "linked_dataset_count": str(len(dataset_ids)),
            "dataset_item_count": item_count,
            "task_count": task_count,
            "linked_task_count": linked_count,
            "task_creation_active_jobs": str(
                job_counts.get("pending", 0) + job_counts.get("running", 0)
            ),
            "task_creation_failed_jobs": str(job_counts.get("failed", 0)),
            "latest_task_creation_status": latest,
            "batch_count": str(len(batches)),
            "nonempty_batch_count": str(nonempty),
            "executable_batch_count": str(executable),
            "assigned_batch_count": str(assigned),
            "active_annotator_count": str(len(annotators)),
            "active_reviewer_count": str(len(reviewers)),
            "activated_batch_count": str(executable),
        }
