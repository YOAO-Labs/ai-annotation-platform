"""Pure scope decisions for Notifications' current delivery readers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol
import uuid

from .publication_contract import PublicationAuthorization, publication_allowed

Pair = tuple[uuid.UUID, uuid.UUID]
DeliveryScope = tuple[bool, uuid.UUID | None, bool]
GLOBAL_ASYNC_JOB_KINDS = frozenset({"audit_archive", "dataset_import"})
PRIVATE_MIGRATION_JOB_KINDS = frozenset(
    {"storage_migration_inventory", "storage_migration_copy"}
)
_ACTUAL_TARGET_TYPES = frozenset({"export", "async_job"})


class NotificationDeliveryReadData(Protocol):
    async def job_scopes(
        self, job_ids: set[uuid.UUID]
    ) -> Iterable[tuple[uuid.UUID, uuid.UUID | None, str]]: ...

    async def member_pairs(self, pairs: set[Pair]) -> Iterable[Pair]: ...

    async def reviewer_pairs(self, pairs: set[Pair]) -> Iterable[Pair]: ...

    async def owner_pairs(self, pairs: set[Pair]) -> Iterable[Pair]: ...

    async def super_pairs(self, pairs: set[Pair]) -> Iterable[Pair]: ...

    async def global_super_admins(
        self, user_ids: set[uuid.UUID]
    ) -> Iterable[uuid.UUID]: ...


def notification_project_id(row) -> uuid.UUID | None:
    """Use the existing dict-only payload UUID parser without normalization."""

    payload = row.payload
    if not isinstance(payload, dict):
        return None
    raw = payload.get("project_id")
    if raw is None:
        return None
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError):
        return None


def unrestricted_delivery_indices(rows) -> set[int]:
    """Fail closed after a scope-read failure exactly as the API publisher did."""

    allowed: set[int] = set()
    for index, row in enumerate(rows):
        payload = row.payload if isinstance(row.payload, dict) else {}
        restricted = row.target_type in _ACTUAL_TARGET_TYPES or "project_id" in payload
        if not restricted:
            allowed.add(index)
    return allowed


class NotificationDeliveryReadPolicy:
    """Resolve delivery scope and authorization facts from native owner reads."""

    def __init__(self, data: NotificationDeliveryReadData):
        self.data = data
        self._migration_inventory_rows: dict[int, bool] = {}

    async def resolve_scopes(self, rows) -> list[DeliveryScope]:
        self._migration_inventory_rows = {}
        job_ids = {
            row.target_id for row in rows if row.target_type in _ACTUAL_TARGET_TYPES
        }
        jobs: dict[uuid.UUID, tuple[uuid.UUID | None, str]] = {}
        if job_ids:
            jobs = {
                job_id: (project_id, kind)
                for job_id, project_id, kind in await self.data.job_scopes(job_ids)
            }

        scopes: list[DeliveryScope] = []
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            target_type = row.target_type
            if target_type in _ACTUAL_TARGET_TYPES:
                job = jobs.get(row.target_id)
                if job is None:
                    job_project, job_kind = None, None
                else:
                    job_project, job_kind = job
                if target_type == "export":
                    restricted = True
                    export_required = True
                else:
                    restricted = job is None or job_project is not None
                    # The actual job kind wins; a retained private kind survives
                    # job-row retention through the payload fallback.
                    effective_kind = (
                        job_kind
                        if job is not None
                        else (
                            payload.get("kind")
                            if isinstance(payload.get("kind"), str)
                            else None
                        )
                    )
                    migration_inventory = effective_kind in PRIVATE_MIGRATION_JOB_KINDS
                    if migration_inventory:
                        restricted = True
                    self._migration_inventory_rows[id(row)] = migration_inventory
                    export_required = job is not None and job_kind == "export"
                project_id = job_project
            else:
                restricted = "project_id" in payload
                export_required = False
                project_id = notification_project_id(row)
            scopes.append((restricted, project_id, export_required))
        return scopes

    async def authorization_facts(
        self,
        rows,
        scopes: list[DeliveryScope],
    ) -> list[PublicationAuthorization]:
        pairs = {
            (project_id, row.user_id)
            for row, (restricted, project_id, _export_required) in zip(rows, scopes)
            if restricted and project_id is not None
        }
        member_pairs: set[Pair] = set()
        reviewer_pairs: set[Pair] = set()
        owner_pairs: set[Pair] = set()
        super_pairs: set[Pair] = set()
        if pairs:
            # Keep these as separate owner reads in the original evaluation order.
            member_pairs = set(await self.data.member_pairs(pairs))
            reviewer_pairs = set(await self.data.reviewer_pairs(pairs))
            owner_pairs = set(await self.data.owner_pairs(pairs))
            super_pairs = set(await self.data.super_pairs(pairs))
        projectless_users = {
            row.user_id
            for row, (restricted, project_id, _export_required) in zip(rows, scopes)
            if restricted
            and project_id is None
            and self._migration_inventory_rows.get(id(row), False)
        }
        projectless_supers = (
            set(await self.data.global_super_admins(projectless_users))
            if projectless_users
            else set()
        )

        facts: list[PublicationAuthorization] = []
        for index, (restricted, project_id, export_required) in enumerate(scopes):
            member = reviewer = owner = super_admin = False
            if restricted and project_id is not None:
                pair = (project_id, rows[index].user_id)
                member = pair in member_pairs
                reviewer = pair in reviewer_pairs
                owner = pair in owner_pairs
                super_admin = pair in super_pairs
            elif (
                restricted
                and project_id is None
                and self._migration_inventory_rows.get(id(rows[index]), False)
            ):
                super_admin = rows[index].user_id in projectless_supers
            facts.append(
                PublicationAuthorization(
                    restricted=restricted,
                    project_id=project_id,
                    export_required=export_required,
                    member=member,
                    reviewer=reviewer,
                    owner=owner,
                    super_admin=super_admin,
                )
            )
        return facts

    async def allowed_indices(
        self,
        rows,
        scopes: list[DeliveryScope],
    ) -> set[int]:
        facts = await self.authorization_facts(rows, scopes)
        return {
            index
            for index, authorization in enumerate(facts)
            if publication_allowed(authorization)
        }
