"""Read registration status using the same defaults as the Recovery owner."""

from __future__ import annotations

from typing import Protocol

from .contract import IdentityUnavailable
from .registration_contract import (
    REGISTRATION_STATUS_PROTOCOL,
    RegistrationStatusFallbacks,
    RegistrationStatusReply,
    RegistrationStatusRequest,
    RegistrationStatusValue,
    decode_registration_status_reply,
)


class RegistrationStatusData(Protocol):
    async def read_registration_status(
        self, fallbacks: dict
    ) -> dict | RegistrationStatusReply: ...


class RegistrationStatusReader:
    def __init__(
        self,
        data: RegistrationStatusData | None,
        *,
        allow_open_registration: bool,
        frontend_base_url: str,
    ):
        self.data = data
        try:
            self.fallbacks = RegistrationStatusFallbacks(
                allow_open_registration=allow_open_registration,
                frontend_base_url=frontend_base_url,
            )
        except (ValueError, TypeError):
            raise IdentityUnavailable() from None

    async def read(self, request: RegistrationStatusRequest) -> RegistrationStatusReply:
        if not isinstance(request, RegistrationStatusRequest):
            raise IdentityUnavailable()
        if request.operation == "read_deployment_default":
            return RegistrationStatusReply(
                schema=REGISTRATION_STATUS_PROTOCOL,
                operation=request.operation,
                ok=True,
                data=RegistrationStatusValue(
                    allow_open_registration=self.fallbacks.allow_open_registration
                ),
            )
        if self.data is None:
            raise IdentityUnavailable()
        raw = await self.data.read_registration_status(
            self.fallbacks.model_dump(mode="json")
        )
        return decode_registration_status_reply(raw, request)
