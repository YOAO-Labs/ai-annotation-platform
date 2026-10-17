"""Administrator invitation query, export, and management routes."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.invitation_models import (
    Invitation,
    InvitationEmailSent,
    InvitationPage,
    InvitationResend,
    InvitationScope,
    InvitationStats,
    InvitationStatus,
)

IdLike = str | UUID
DateLike = datetime | str
InvitationExportFormat = Literal["csv", "json"]


def _filters(
    *,
    status: InvitationStatus,
    scope: InvitationScope,
    project_id: IdLike | None,
    role: str | None,
    email: str | None,
    search: str | None,
    created_from: DateLike | None,
    created_to: DateLike | None,
) -> dict[str, str]:
    values: dict[str, str | None] = {
        "status": status,
        "scope": scope,
        "project_id": str(project_id) if project_id is not None else None,
        "role": role,
        "email": email,
        "search": search,
        "created_from": (
            created_from.isoformat()
            if isinstance(created_from, datetime)
            else created_from
        ),
        "created_to": (
            created_to.isoformat() if isinstance(created_to, datetime) else created_to
        ),
    }
    return {key: value for key, value in values.items() if value is not None}


class Invitations:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        status: InvitationStatus = "all",
        scope: InvitationScope = "me",
    ) -> list[Invitation]:
        """Legacy unpaginated invitation list; use query() for bounded reads."""
        resp = self._http.request(
            "GET", "/invitations", params={"status": status, "scope": scope}
        )
        return [Invitation.model_validate(item) for item in resp.json()]

    def query(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: InvitationStatus = "all",
        scope: InvitationScope = "me",
        project_id: IdLike | None = None,
        role: str | None = None,
        email: str | None = None,
        search: str | None = None,
        created_from: DateLike | None = None,
        created_to: DateLike | None = None,
    ) -> InvitationPage:
        params = {
            "page": page,
            "page_size": page_size,
            **_filters(
                status=status,
                scope=scope,
                project_id=project_id,
                role=role,
                email=email,
                search=search,
                created_from=created_from,
                created_to=created_to,
            ),
        }
        resp = self._http.request("GET", "/invitations/query", params=params)
        return InvitationPage.model_validate(resp.json())

    def stats(
        self,
        *,
        status: InvitationStatus = "all",
        scope: InvitationScope = "me",
        project_id: IdLike | None = None,
        role: str | None = None,
        email: str | None = None,
        search: str | None = None,
        created_from: DateLike | None = None,
        created_to: DateLike | None = None,
    ) -> InvitationStats:
        resp = self._http.request(
            "GET",
            "/invitations/stats",
            params=_filters(
                status=status,
                scope=scope,
                project_id=project_id,
                role=role,
                email=email,
                search=search,
                created_from=created_from,
                created_to=created_to,
            ),
        )
        return InvitationStats.model_validate(resp.json())

    def export(
        self,
        out: str | Path,
        *,
        format: InvitationExportFormat = "csv",
        status: InvitationStatus = "all",
        scope: InvitationScope = "me",
        project_id: IdLike | None = None,
        role: str | None = None,
        email: str | None = None,
        search: str | None = None,
        created_from: DateLike | None = None,
        created_to: DateLike | None = None,
    ) -> Path:
        """Stream a CSV or JSON export atomically to the caller's path."""
        params = {
            "format": format,
            **_filters(
                status=status,
                scope=scope,
                project_id=project_id,
                role=role,
                email=email,
                search=search,
                created_from=created_from,
                created_to=created_to,
            ),
        }
        return self._http.stream_download(
            "/api/v1/invitations/export", Path(out), params=params
        )

    def revoke(self, invitation_id: IdLike) -> None:
        self._http.request("DELETE", f"/invitations/{invitation_id}")

    def resend(self, invitation_id: IdLike) -> InvitationResend:
        """Rotate/revive an invitation token; this route does not send email."""
        resp = self._http.request("POST", f"/invitations/{invitation_id}/resend")
        return InvitationResend.model_validate(resp.json())

    def send_email(self, invitation_id: IdLike) -> InvitationEmailSent:
        """Send the existing, unchanged invitation token through configured SMTP."""
        resp = self._http.request("POST", f"/invitations/{invitation_id}/send-email")
        return InvitationEmailSent.model_validate(resp.json())
