"""Membership lifecycle coordinator over owner-controlled transactions."""

from __future__ import annotations

from typing import AsyncContextManager, Protocol

from .contract import (
    MEMBERSHIP_NATIVE_PROTOCOL,
    MembershipError,
    MembershipPrepareFacts,
    MembershipReply,
    MembershipRequest,
    MembershipUnavailable,
    NativeMembershipApplyRequest,
    NativeMembershipApplySuccess,
    NativeMembershipFailure,
    NativeMembershipPrepareSuccess,
    decode_membership_request,
    decode_native_membership_reply,
    membership_failure_reply,
    membership_success_reply,
)
from .policy import (
    add_member_error,
    change_role_error,
    preview_role_change_reply,
    remove_member_error,
    validate_request_role,
)


class MembershipTransaction(Protocol):
    async def prepare_membership_command(self, request_json: dict) -> dict | bytes: ...

    async def apply_membership_command(self, apply_json: dict) -> dict | bytes: ...


class MembershipData(Protocol):
    def transaction(
        self, *, write: bool
    ) -> AsyncContextManager[MembershipTransaction]: ...


class MembershipCommandFailure(Exception):
    def __init__(self, error: MembershipError):
        self.error = error
        super().__init__("Membership operation rejected")


class MembershipManager:
    """Run a membership request without owning storage or HTTP concerns."""

    def __init__(self, data: MembershipData):
        self.data = data

    async def execute(
        self, request: MembershipRequest | dict | bytes | str
    ) -> MembershipReply:
        request = decode_membership_request(request)

        early_error = validate_request_role(request)
        if early_error is not None:
            return membership_failure_reply(request, early_error)

        deferred_error: MembershipError | None = None
        reply: MembershipReply | None = None
        write = request.operation != "preview_role_change"
        try:
            async with self.data.transaction(write=write) as transaction:
                prepared = decode_native_membership_reply(
                    request,
                    await transaction.prepare_membership_command(
                        request.model_dump(mode="json", by_alias=True)
                    ),
                )
                if isinstance(prepared, NativeMembershipFailure):
                    deferred_error = prepared.error
                    self._fail_transaction(deferred_error)
                elif isinstance(prepared, NativeMembershipPrepareSuccess):
                    if prepared.data.kind == "replay":
                        if request.operation == "preview_role_change":
                            raise MembershipUnavailable()
                        reply = prepared.data.reply
                    else:
                        facts = prepared.data
                        if request.operation == "preview_role_change":
                            reply = preview_role_change_reply(request, facts)
                        else:
                            deferred_error = self._policy_error(request, facts)
                            if deferred_error is not None:
                                self._fail_transaction(deferred_error)
                            else:
                                applied = await self._apply(request, facts, transaction)
                                if isinstance(applied, MembershipError):
                                    deferred_error = applied
                                    self._fail_transaction(deferred_error)
                                else:
                                    reply = applied
                else:
                    # `prepare_membership_command` must return facts, a frozen
                    # replay, or a failure; an applied result is out of phase.
                    raise MembershipUnavailable()
        except MembershipCommandFailure as exc:
            deferred_error = exc.error

        if deferred_error is not None:
            return membership_failure_reply(request, deferred_error)
        if reply is None:
            raise MembershipUnavailable()
        return reply

    @staticmethod
    def _fail_transaction(error: MembershipError) -> None:
        raise MembershipCommandFailure(error)

    @staticmethod
    def _policy_error(
        request: MembershipRequest, facts: MembershipPrepareFacts
    ) -> MembershipError | None:
        if request.operation == "add_member":
            return add_member_error(request, facts)
        if request.operation == "change_role":
            return change_role_error(request, facts)
        if request.operation == "remove_member":
            return remove_member_error(request, facts)
        raise MembershipUnavailable()

    @staticmethod
    async def _apply(
        request: MembershipRequest,
        facts: MembershipPrepareFacts,
        transaction: MembershipTransaction,
    ) -> MembershipReply | MembershipError:
        command = NativeMembershipApplyRequest(
            schema=MEMBERSHIP_NATIVE_PROTOCOL,
            request=request,
            transaction_id=facts.transaction_id,
        )
        native = decode_native_membership_reply(
            request,
            await transaction.apply_membership_command(
                command.model_dump(mode="json", by_alias=True)
            ),
        )
        if isinstance(native, NativeMembershipFailure):
            return native.error
        if not isinstance(native, NativeMembershipApplySuccess):
            raise MembershipUnavailable()
        return membership_success_reply(request, native.data.result)
