"""Portable Project member-directory query orchestration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import AsyncContextManager, Protocol
import uuid

from .directory_query_contract import (
    DIRECTORY_QUERY_PROTOCOL,
    DirectoryQueryError,
    DirectoryQueryFailure,
    DirectoryQueryMentionOwnerFacts,
    DirectoryQueryMentionSuperadminsFacts,
    DirectoryQueryMemberRowsFacts,
    DirectoryQueryProjectId,
    DirectoryQueryRequest,
    DirectoryQuerySuccess,
    DirectoryQueryUnavailable,
    DirectoryQueryUserId,
    decode_directory_query_request,
    decode_directory_query_step,
)
from .directory_query_values import (
    decode_directory_query_reply,
    decode_directory_member_rows,
    decode_directory_mention_member_rows,
    directory_query_data_json,
    member_response_data,
    mention_response_data,
)
from aap_workspace_contract.project_query_contract import (
    ProjectQueryRejected,
    ProjectQueryUnavailable,
)
from .project_queries import ProjectQueryProfilesUnavailable
from aap_workspace_contract.project_read_authority import accessible_project_row


class DirectoryQueryProfile(Protocol):
    id: uuid.UUID
    name: str
    email: str
    role: str | None
    avatar_initial: str
    avatar_ref: str | None


class DirectoryQueryProfiles(Protocol):
    async def resolve(
        self, user_ids: list[uuid.UUID]
    ) -> Mapping[str, DirectoryQueryProfile]: ...


class DirectoryQueryTransaction(Protocol):
    async def step(self, request, action, parameters=None): ...


class DirectoryQueryData(Protocol):
    def transaction(self) -> AsyncContextManager[DirectoryQueryTransaction]: ...


class DirectoryQueryManager:
    """Run only fixed read steps and return legacy-shaped data as opaque JSON text."""

    def __init__(
        self,
        data: DirectoryQueryData,
        profiles: DirectoryQueryProfiles | None = None,
    ) -> None:
        self.data = data
        self.profiles = profiles

    async def execute(self, payload) -> DirectoryQuerySuccess | DirectoryQueryFailure:
        request = decode_directory_query_request(payload)
        try:
            async with self.data.transaction() as transaction:
                try:
                    row = await accessible_project_row(request, transaction, self._step)
                except ProjectQueryUnavailable:
                    raise DirectoryQueryUnavailable() from None

                if request.operation == "list_members":
                    data = await self._list_members(request, transaction)
                else:
                    data = await self._mention_candidates(request, transaction, row)
        except ProjectQueryProfilesUnavailable:
            return self._failure(
                request,
                status_code=503,
                detail="身份资料服务暂不可用",
                headers=None,
            )
        except ProjectQueryRejected as error:
            return self._failure(
                request,
                status_code=error.error.status_code,
                detail=error.error.detail,
                headers=(
                    dict(error.error.headers)
                    if error.error.headers is not None
                    else None
                ),
            )

        reply = DirectoryQuerySuccess(
            schema=DIRECTORY_QUERY_PROTOCOL,
            ok=True,
            request=request,
            data_json=directory_query_data_json(data),
        )
        return decode_directory_query_reply(request, reply)

    @staticmethod
    async def _step(request, transaction, action, parameters=None):
        raw = await transaction.step(request, action, parameters=parameters)
        return decode_directory_query_step(request, action, parameters, raw).facts

    async def _list_members(self, request, transaction):
        facts: DirectoryQueryMemberRowsFacts = await self._step(
            request,
            transaction,
            "member_rows",
            DirectoryQueryProjectId(project_id=request.target_id),
        )
        # Decode every persisted field (including timestamps) before Identity I/O.
        members = decode_directory_member_rows(
            facts.rows_json, target_id=request.target_id
        )
        if not members:
            return []
        if self.profiles is None:
            raise ProjectQueryProfilesUnavailable()
        briefs = await self.profiles.resolve([member.user_id for member in members])
        result = []
        for member in members:
            profile = briefs.get(str(member.user_id))
            if profile is None:
                continue
            if profile.id != member.user_id:
                raise DirectoryQueryUnavailable()
            result.append(member_response_data(member, profile))
        return result

    async def _mention_candidates(self, request, transaction, row):
        seen: set[uuid.UUID] = set()
        result = []

        def add(user_id, name, email, kind):
            if user_id in seen:
                return
            seen.add(user_id)
            result.append(mention_response_data(user_id, name, email, kind))

        owner_id = row["owner_id"]
        if owner_id is not None:
            owner_facts: DirectoryQueryMentionOwnerFacts = await self._step(
                request,
                transaction,
                "mention_owner",
                DirectoryQueryUserId(user_id=uuid.UUID(owner_id)),
            )
            owner = owner_facts.user
            if owner is not None and owner.is_active:
                add(owner.id, owner.name, owner.email, "owner")

        superadmin_facts: DirectoryQueryMentionSuperadminsFacts = await self._step(
            request, transaction, "mention_superadmins"
        )
        for account in superadmin_facts.accounts:
            add(account.id, account.name, account.email, "super_admin")

        members_facts: DirectoryQueryMemberRowsFacts = await self._step(
            request,
            transaction,
            "mention_members",
            DirectoryQueryProjectId(project_id=request.target_id),
        )
        # Decode all nine member columns before UUID dedupe, including inactive users.
        members = decode_directory_mention_member_rows(
            members_facts.rows_json, target_id=request.target_id
        )
        for candidate in members:
            add(
                candidate.member.user_id,
                candidate.user_name,
                candidate.user_email,
                "member",
            )
        return result

    @staticmethod
    def _failure(
        request: DirectoryQueryRequest,
        *,
        status_code: int,
        detail: str,
        headers: dict[str, str] | None,
    ) -> DirectoryQueryFailure:
        error = DirectoryQueryError(
            status_code=status_code,
            detail=detail,
            headers=headers,
        )
        return decode_directory_query_reply(
            request,
            DirectoryQueryFailure(
                schema=DIRECTORY_QUERY_PROTOCOL,
                ok=False,
                request=request,
                error=error,
            ),
        )
