"""Bind manifest admission to the caller transaction and compose its owner."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Protocol

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_input_manifests import contract as c
from aap_assets_inputs import contract as inputs
from app.config import settings
from app.services.accepted_inputs import input_gateway


class SqlAlchemyManifestDatabase:
    """Only published capabilities; validation completes inside a savepoint."""

    def __init__(self, db: AsyncSession, *, commit=True, allowed_roles=("control",)):
        self.db, self.commit = db, commit
        self.allowed_roles = frozenset(allowed_roles)

    async def call(self, operation: str, request: BaseModel) -> BaseModel:
        definition = c.OPERATIONS.get(operation)
        if (
            definition is None
            or type(request) is not definition.request
            or definition.role not in self.allowed_roles
        ):
            raise c.ManifestFailure("invalid_request")
        if len(c.json_bytes(request)) > c.REQUEST_BYTES:
            raise c.ManifestFailure("resource_budget_exceeded")
        parameters, placeholders = {}, []
        for name, kind in zip(definition.arguments, definition.sql_types, strict=True):
            value = getattr(request, name)
            parameters[name] = (
                c.json_bytes(value).decode("utf-8")
                if kind == "jsonb" and value is not None
                else value
            )
            placeholders.append(f"CAST(:{name} AS {kind})")
        try:
            await self.db.flush()
            async with self.db.begin_nested():
                raw = await self.db.scalar(
                    text(
                        f"SELECT aap_assets_v1.{operation}({', '.join(placeholders)})"
                    ),
                    parameters,
                )
                result = c.decode_reply(raw, definition.result)
                c.require_scope(request, result)
            if self.commit:
                await self.db.commit()
            return result
        except c.ManifestFailure:
            if self.commit:
                await self.db.rollback()
            raise
        except SQLAlchemyError as exc:
            if self.commit:
                await self.db.rollback()
            cause = getattr(exc, "orig", exc)
            state = getattr(cause, "sqlstate", None) or getattr(cause, "pgcode", None)
            raise c.ManifestFailure(
                "manifest_busy"
                if state in {"55P03", "40001", "40P01"}
                else "storage_unavailable"
            ) from None
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            if self.commit:
                await self.db.rollback()
            raise c.ManifestFailure("invalid_response") from None


class InputManifestCapturePort(Protocol):
    async def capture(self, request: c.CaptureManifestRequest) -> c.ManifestHeader: ...


class InputManifestCaptureFactory(Protocol):
    def __call__(self, db: AsyncSession) -> InputManifestCapturePort: ...


class _NativeManifestCapture:
    def __init__(self, db):
        self.database = SqlAlchemyManifestDatabase(
            db, commit=False, allowed_roles={"workflow"}
        )

    async def capture(self, request):
        return await self.database.call(
            "capture_accepted_image_input_manifest", request
        )


def get_input_manifest_capture() -> InputManifestCaptureFactory:
    """Admission is SQL-only and shares the Job's original transaction."""
    return _NativeManifestCapture


class _IndependentManifestDatabase:
    def __init__(self, session_factory=None):
        self.session_factory = session_factory

    async def call(self, operation, request):
        from app.db.base import async_session

        async with (self.session_factory or async_session)() as db:
            return await SqlAlchemyManifestDatabase(db).call(operation, request)


class InputManifestGateway:
    def __init__(self, provider, inputs_gateway, *, preparation_limit=c.PAGE_SIZE):
        if not 1 <= preparation_limit <= c.PAGE_SIZE:
            raise ValueError("invalid manifest preparation limit")
        self.provider, self.inputs = provider, inputs_gateway
        self.preparation_limit = preparation_limit

    async def page(self, request: c.ManifestPageRequest) -> c.ManifestPage:
        result = await self.provider.page(request)
        self._require_scope(request, result)
        return result

    @staticmethod
    def _require_scope(request, result):
        try:
            c.require_scope(request, result)
        except (ValueError, TypeError, AttributeError):
            raise c.ManifestFailure("invalid_response") from None

    async def ensure_sealed(
        self, scope: c.ManifestScope, *, selection_digest: str
    ) -> c.ManifestHeader:
        header = await self.provider.record(scope)
        self._require_scope(scope, header)
        if header.selection_digest != selection_digest:
            raise c.ManifestFailure("manifest_scope_mismatch")
        if header.status == "sealed":
            return header

        # Each delivery attempts at most one bounded page of unfinished work.
        # Completed members do not consume that budget: an arbitrarily large
        # selection can progress on replay without storing a full Task/Input map.
        cursor, attempted = 0, 0
        while cursor < header.selected_count:
            page_request = c.ManifestPageRequest(
                **scope.model_dump(),
                selection_digest=selection_digest,
                cursor=cursor,
            )
            page = await self.page(page_request)
            for member in page.members:
                if member.state in {"ready", "failed"}:
                    continue
                if member.input is not None:
                    current = await self._record_input(member.input)
                    if current.status in {"ready", "failed", "expired"}:
                        continue
                if attempted >= self.preparation_limit:
                    raise c.ManifestFailure("manifest_not_ready")
                attempted += 1
                if member.state == "unprepared":
                    request = c.PrepareManifestMemberRequest(
                        **scope.model_dump(),
                        selection_digest=selection_digest,
                        ordinal=member.ordinal,
                        task_id=member.task_id,
                    )
                    receipt = await self.provider.prepare(request)
                    self._require_scope(request, receipt)
                    member = receipt.member
                if member.state == "failed" or member.input is None:
                    continue
                try:
                    await self.inputs.ensure_reference(
                        self._input_request(member.input)
                    )
                except inputs.InputFailure as exc:
                    # The Input owner records terminal copy outcomes. Seal reads
                    # those rows itself. Authority and source errors still stop
                    # this operation rather than becoming readiness deferrals.
                    if not exc.code.endswith("_busy") and exc.code not in {
                        "input_not_ready",
                        "storage_unavailable",
                    }:
                        if exc.code not in {
                            "source_missing",
                            "source_changed",
                            "source_not_ready",
                            "not_image",
                            "input_expired",
                            "input_corrupt",
                            "resource_budget_exceeded",
                        }:
                            raise c.ManifestFailure(exc.code) from None
                        current = await self._record_input(member.input)
                        if current.status not in {"ready", "failed", "expired"}:
                            raise c.ManifestFailure(exc.code) from None
            if page.next_cursor is None:
                break
            cursor = page.next_cursor

        request = c.SealManifestRequest(
            **scope.model_dump(), selection_digest=selection_digest
        )
        sealed = await self.provider.seal(request)
        self._require_scope(request, sealed)
        return sealed

    async def _record_input(self, record):
        request = self._input_request(record)
        try:
            current = await self.inputs.record(request)
            inputs.require_scope(request, current)
            return current
        except inputs.InputFailure as exc:
            raise c.ManifestFailure(exc.code) from None
        except (ValueError, TypeError, AttributeError):
            raise c.ManifestFailure("invalid_response") from None

    @staticmethod
    def _input_request(record):
        return inputs.InputRequest(
            input_id=record.input_id, job_id=record.job_id, task_id=record.task_id
        )


@asynccontextmanager
async def input_manifest_gateway(*, session_factory=None):
    async with input_gateway(session_factory=session_factory) as original_inputs:
        if settings.assets_media_url:
            from aap_assets_input_manifests.client import ManifestClient

            async with ManifestClient(
                settings.assets_media_url, settings.assets_media_token
            ) as client:
                yield InputManifestGateway(client, original_inputs)
            return

        from aap_assets_input_manifests_runtime.control import ManifestControl

        provider = ManifestControl(
            _IndependentManifestDatabase(session_factory),
            original_inputs.provider.images,
        )
        yield InputManifestGateway(provider, original_inputs)
