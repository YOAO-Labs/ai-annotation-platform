"""Portable list, detail, and post-commit projection for ProjectTemplate."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any, AsyncContextManager, Protocol

from .configuration_policy import ConfigurationPolicyError, assert_template_visible
from aap_workspace_contract.wire import _reject_constant, _unique_object
from .template_query_contract import (
    TEMPLATE_ROW_COLUMNS,
    TemplateQueryAction,
    TemplateQueryMissing,
    TemplateQueryOrganizationIdsStep,
    TemplateQueryCreatorNameStep,
    TemplateQueryGetRowStep,
    TemplateQueryListRowsStep,
    TemplateQueryProjectionRowStep,
    TemplateQueryReply,
    TemplateQueryRequest,
    TemplateQueryStepReply,
    TemplateQueryUnavailable,
    decode_template_query_filters,
    decode_template_query_reply,
    decode_template_query_request,
    decode_template_query_step_reply,
    template_query_failure_reply,
    template_query_success_reply,
)


class TemplateQueryTransaction(Protocol):
    async def step(
        self,
        request: TemplateQueryRequest,
        action: TemplateQueryAction,
        parameters: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> Any: ...


class TemplateQueryData(Protocol):
    def transaction(self) -> AsyncContextManager[TemplateQueryTransaction]: ...


class TemplateQueryManager:
    """Run the legacy template read phases over a fixed Workspace data port."""

    def __init__(self, data: TemplateQueryData):
        self.data = data

    async def execute(
        self, request: TemplateQueryRequest | dict | bytes | str
    ) -> TemplateQueryReply:
        request = decode_template_query_request(request)
        missing = False
        async with self.data.transaction() as transaction:
            try:
                if request.operation == "list_templates":
                    data_json = await self._list(request, transaction)
                elif request.operation == "get_template":
                    data_json = await self._get(request, transaction)
                elif request.operation == "get_template_projection":
                    data_json = await self._project(request, transaction)
                else:
                    raise TemplateQueryUnavailable()
                reply = template_query_success_reply(request, data_json)
            except TemplateQueryMissing:
                missing = True
        if missing:
            return template_query_failure_reply(request)
        return decode_template_query_reply(request, reply)

    async def _list(
        self,
        request: TemplateQueryRequest,
        transaction: TemplateQueryTransaction,
    ) -> str:
        actor = request.admitted_actor
        organization_ids: list[uuid.UUID] = []
        if actor.role != "super_admin":
            facts = await self._step(
                request,
                transaction,
                "organization_ids",
            )
            organization_ids = facts.facts.organization_ids

        # Keep the filter string opaque until after the non-super-admin org read.
        filters = decode_template_query_filters(request)
        facts = await self._step(
            request,
            transaction,
            "list_rows",
            parameters={
                "organization_ids": [
                    str(organization_id) for organization_id in organization_ids
                ]
            },
            filters=filters.model_dump(mode="json"),
        )
        if not isinstance(facts, TemplateQueryListRowsStep):
            raise TemplateQueryUnavailable()
        rows = self._decode_rows(facts.facts.rows_json)
        for row in rows:
            creator_id = uuid.UUID(row["created_by"])
            row["created_by_name"] = await self._creator_name(
                request, transaction, creator_id
            )
        return self._encode_data(rows)

    async def _get(
        self,
        request: TemplateQueryRequest,
        transaction: TemplateQueryTransaction,
    ) -> str:
        facts = await self._step(request, transaction, "get_row")
        if not isinstance(facts, TemplateQueryGetRowStep):
            raise TemplateQueryUnavailable()
        row = self._decode_row(
            facts.facts.row_json,
            target_id=request.target_id,
            visibility_fields=True,
        )
        if row is None:
            raise TemplateQueryMissing()

        actor = request.admitted_actor
        organization_ids: list[uuid.UUID] = []
        organization_id = row["organization_id"]
        if (
            actor.role != "super_admin"
            and row["scope"] == "organization"
            and organization_id is not None
        ):
            org_facts = await self._step(
                request,
                transaction,
                "organization_ids",
            )
            if not isinstance(org_facts, TemplateQueryOrganizationIdsStep):
                raise TemplateQueryUnavailable()
            organization_ids = org_facts.facts.organization_ids

        try:
            assert_template_visible(
                {
                    "scope": row["scope"],
                    "organization_id": (
                        uuid.UUID(organization_id)
                        if organization_id is not None
                        else None
                    ),
                    "created_by": uuid.UUID(row["created_by"]),
                },
                {"id": actor.id, "role": actor.role},
                organization_ids,
            )
        except ConfigurationPolicyError as error:
            if error.status_code == 404:
                raise TemplateQueryMissing() from error
            raise TemplateQueryUnavailable() from error

        row["created_by_name"] = await self._creator_name(
            request, transaction, uuid.UUID(row["created_by"])
        )
        return self._encode_data(row)

    async def _project(
        self,
        request: TemplateQueryRequest,
        transaction: TemplateQueryTransaction,
    ) -> str:
        facts = await self._step(request, transaction, "projection_row")
        if not isinstance(facts, TemplateQueryProjectionRowStep):
            raise TemplateQueryUnavailable()
        row = self._decode_row(
            facts.facts.row_json,
            target_id=request.target_id,
        )
        if row is None:
            return "null"
        row["created_by_name"] = await self._creator_name(
            request, transaction, uuid.UUID(row["created_by"])
        )
        return self._encode_data(row)

    async def _creator_name(
        self,
        request: TemplateQueryRequest,
        transaction: TemplateQueryTransaction,
        creator_id: uuid.UUID,
    ) -> str | None:
        facts = await self._step(
            request,
            transaction,
            "creator_name",
            parameters={"creator_id": str(creator_id)},
        )
        if not isinstance(facts, TemplateQueryCreatorNameStep):
            raise TemplateQueryUnavailable()
        return facts.facts.name

    async def _step(
        self,
        request: TemplateQueryRequest,
        transaction: TemplateQueryTransaction,
        action: TemplateQueryAction,
        *,
        parameters: dict[str, Any] | None = None,
        filters: dict[str, Any] | None = None,
    ) -> TemplateQueryStepReply:
        raw = await transaction.step(
            request,
            action,
            parameters=parameters,
            filters=filters,
        )
        return decode_template_query_step_reply(request, action, raw)

    @staticmethod
    def _decode_rows(rows_json: str) -> list[dict[str, Any]]:
        try:
            rows = json.loads(
                rows_json,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            if type(rows) is not list:
                raise ValueError("Template list rows must be an array")
            return [TemplateQueryManager._decode_row_object(row) for row in rows]
        except (
            json.JSONDecodeError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            raise TemplateQueryUnavailable() from None

    @staticmethod
    def _decode_row(
        row_json: str | None,
        *,
        target_id: uuid.UUID | None,
        visibility_fields: bool = False,
    ) -> dict[str, Any] | None:
        if row_json is None:
            return None
        try:
            row = json.loads(
                row_json,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
            return TemplateQueryManager._decode_row_object(
                row,
                target_id=target_id,
                visibility_fields=visibility_fields,
            )
        except (
            json.JSONDecodeError,
            ValueError,
            TypeError,
            UnicodeError,
            RecursionError,
        ):
            raise TemplateQueryUnavailable() from None

    @staticmethod
    def _decode_row_object(
        row: object,
        *,
        target_id: uuid.UUID | None = None,
        visibility_fields: bool = False,
    ) -> dict[str, Any]:
        if type(row) is not dict or set(row) != set(TEMPLATE_ROW_COLUMNS):
            raise ValueError(
                "Template row does not match the fixed 25-column projection"
            )
        if type(row["id"]) is not str or type(row["created_by"]) is not str:
            raise ValueError("Template row structural IDs must be UUID strings")
        row_id = uuid.UUID(row["id"])
        uuid.UUID(row["created_by"])
        if target_id is not None and row_id != target_id:
            raise ValueError("Template row ID does not match target")
        if type(row["scope"]) is not str:
            raise ValueError("Template scope must be text")
        if visibility_fields and row["organization_id"] is not None:
            if type(row["organization_id"]) is not str:
                raise ValueError("Template organization ID must be a UUID string")
            uuid.UUID(row["organization_id"])
        return row

    @staticmethod
    def _encode_data(value: Mapping[str, Any] | list[Mapping[str, Any]]) -> str:
        try:
            return json.dumps(
                value,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise TemplateQueryUnavailable() from None


__all__ = [
    "TemplateQueryData",
    "TemplateQueryManager",
    "TemplateQueryTransaction",
]
