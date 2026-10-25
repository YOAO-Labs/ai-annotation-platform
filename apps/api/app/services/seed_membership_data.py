"""Bind the seed's staged membership writes to its existing flush/commit points."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import event, inspect, text
from sqlalchemy.orm import make_transient_to_detached
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import StaleDataError

from aap_workspace.seed_member_contract import (
    SEED_MEMBER_PROTOCOL,
    SeedMemberInsert,
    SeedMemberUpdate,
    SeedMemberWriteRequest,
    decode_seed_member_write_reply,
)
from aap_workspace.seed_member_assignment_contract import (
    decode_seed_member_assignment_reply,
)
from app.db.models.project_member import ProjectMember

_UNKNOWN = object()
_WRITE = text("SELECT aap_workspace_v1.write_seed_members(CAST(:request AS jsonb))")
_PIN_ASSIGNMENT = text(
    "SELECT aap_workspace_v1.pin_seed_member_assignment(CAST(:member_id AS uuid),CAST(:assigned_at AS timestamptz))"
)


def _original(member, key):
    history = inspect(member).attrs[key].history
    if history.has_changes():
        return history.deleted[0] if history.deleted else _UNKNOWN
    return member.__dict__[key]


@dataclass
class _Change:
    member: ProjectMember
    original_role: object
    original_assigner: object
    role: str
    assigned_by: uuid.UUID
    original_assigned_at: object = _UNKNOWN
    assigned_at: object = _UNKNOWN


class SeedMembershipWrites:
    """Stage legacy membership fields and optional screenshot assignment time."""

    def __init__(self, db):
        self.db = db
        self.inserts = []
        self.changes = {}
        self.failed_flush = False
        self.assignment_times = {}

    def role(self, member):
        change = self.changes.get(member.id)
        return change.role if change is not None else member.role

    def add(self, *, project_id, user_id, role, assigned_by, assigned_at=_UNKNOWN):
        operation = SeedMemberInsert(
            kind="insert",
            id=uuid.uuid4(),
            project_id=project_id,
            user_id=user_id,
            role=role,
            assigned_by=assigned_by,
        )
        self.inserts.append(operation)
        if assigned_at is not _UNKNOWN:
            self.assignment_times[operation.id] = assigned_at
        return operation

    def change(self, member, *, role, assigned_by, assigned_at=_UNKNOWN):
        change = self.changes.get(member.id)
        if change is None:
            change = _Change(
                member,
                _original(member, "role"),
                _original(member, "assigned_by"),
                role,
                assigned_by,
            )
            self.changes[member.id] = change
        change.role = role
        change.assigned_by = assigned_by
        if assigned_at is not _UNKNOWN:
            if change.assigned_at is _UNKNOWN:
                change.original_assigned_at = _original(member, "assigned_at")
            change.assigned_at = assigned_at
            if change.original_assigned_at != assigned_at:
                self.assignment_times[member.id] = assigned_at
            else:
                self.assignment_times.pop(member.id, None)

    def _flush(self, session):
        self.failed_flush = False
        updates = []
        for member_id, change in self.changes.items():
            write_role = change.original_role != change.role
            write_assigner = change.original_assigner != change.assigned_by
            if write_role or write_assigner:
                updates.append(
                    SeedMemberUpdate(
                        kind="update",
                        id=member_id,
                        role=change.role,
                        assigned_by=change.assigned_by,
                        write_role=write_role,
                        write_assigned_by=write_assigner,
                    )
                )
            # Let the caller flush unrelated attributes without emitting a
            # private-table write for the fields staged through Workspace.
            for key, original in (
                ("role", change.original_role),
                ("assigned_by", change.original_assigner),
            ):
                set_committed_value(
                    change.member,
                    key,
                    change.member.__dict__[key] if original is _UNKNOWN else original,
                )
            if change.assigned_at is not _UNKNOWN:
                original = change.original_assigned_at
                set_committed_value(
                    change.member,
                    "assigned_at",
                    change.member.__dict__["assigned_at"]
                    if original is _UNKNOWN
                    else original,
                )
        try:
            session.flush()
            operations = [*updates, *self.inserts]
            replies = []
            if operations:
                request = SeedMemberWriteRequest(
                    schema=SEED_MEMBER_PROTOCOL, operations=operations
                )
                raw = session.scalar(
                    _WRITE, {"request": request.model_dump_json(by_alias=True)}
                )
                replies = decode_seed_member_write_reply(raw, request)
                if any(reply.kind == "update" and not reply.found for reply in replies):
                    raise StaleDataError(
                        "Seed membership UPDATE expected one row; none matched"
                    )
            pinned = {}
            for member_id, assigned_at in self.assignment_times.items():
                raw = session.scalar(
                    _PIN_ASSIGNMENT,
                    {"member_id": member_id, "assigned_at": assigned_at},
                )
                reply = decode_seed_member_assignment_reply(
                    raw, member_id=member_id, assigned_at=assigned_at
                )
                if not reply.found:
                    raise StaleDataError(
                        "Seed membership assignment expected one row; none matched"
                    )
                pinned[member_id] = reply
        except BaseException:
            self.failed_flush = True
            for change in self.changes.values():
                change.member.role = change.role
                change.member.assigned_by = change.assigned_by
                if change.assigned_at is not _UNKNOWN:
                    change.member.assigned_at = change.assigned_at
            raise
        for operation, reply in zip(operations, replies, strict=True):
            if operation.kind == "insert":
                member = ProjectMember(
                    id=operation.id,
                    project_id=operation.project_id,
                    user_id=operation.user_id,
                    role=operation.role,
                    assigned_by=operation.assigned_by,
                    assigned_at=pinned[operation.id].assigned_at
                    if operation.id in pinned
                    else reply.assigned_at,
                    updated_at=pinned[operation.id].updated_at
                    if operation.id in pinned
                    else reply.updated_at,
                    version=1,
                    weekly_target=None,
                )
                make_transient_to_detached(member)
                session.merge(member, load=False)
            else:
                # Legacy ORM onupdate leaves this server value unloaded until
                # the next SELECT; preserve that cache behavior after native SQL.
                session.expire(self.changes[operation.id].member, ["updated_at"])
        for change in self.changes.values():
            set_committed_value(change.member, "role", change.role)
            set_committed_value(change.member, "assigned_by", change.assigned_by)
            if change.assigned_at is not _UNKNOWN:
                set_committed_value(change.member, "assigned_at", change.assigned_at)
            if change.member.id in pinned:
                session.expire(change.member, ["updated_at"])
        self.inserts.clear()
        self.changes.clear()
        self.assignment_times.clear()

    async def flush(self):
        try:
            await self.db.run_sync(self._flush)
        except BaseException:
            if self.failed_flush:
                await self.db.rollback()
            raise

    async def flush_before_read(self):
        if self.db.autoflush and (self.inserts or self.changes):
            await self.flush()

    async def commit(self):
        # Before-commit keeps both unrelated pending work and duplicate-insert
        # errors at the same caller-owned commit boundary, including no_autoflush.
        callback = self._flush
        event.listen(self.db.sync_session, "before_commit", callback)
        try:
            await self.db.commit()
        except BaseException:
            if self.failed_flush:
                await self.db.rollback()
            raise
        finally:
            event.remove(self.db.sync_session, "before_commit", callback)
