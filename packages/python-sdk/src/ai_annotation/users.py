"""Account management REST resources."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from uuid import UUID

from ai_annotation._http import HttpTransport, _drop_none
from ai_annotation.models import PlatformRole
from ai_annotation.user_management_models import (
    AdminResetPasswordResponse,
    BulkGroupAssignmentPreview,
    BulkGroupAssignmentRequest,
    BulkGroupAssignmentResponse,
    BulkInviteRequest,
    BulkInviteResponse,
    DeleteUserRequest,
    InvitationCreate,
    InvitationCreated,
    OffboardingCommitRequest,
    OffboardingPreview,
    OffboardingResult,
    ReactivateRequest,
    RoleImpactPreview,
    UserPage,
    UserRecord,
    UsersStats,
)

IdLike = str | UUID
UserStatus = Literal["active", "inactive", "all"]


class Users:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        role: str | None = None,
        status: UserStatus = "active",
        project_id: IdLike | None = None,
    ) -> list[UserRecord]:
        """Read the legacy array endpoint.

        Project admins ignore ``project_id``; with ``status="active"`` and
        ``role="employee"`` this endpoint returns the broad assignment-candidate
        list. Use :meth:`query` when filtering to a project is required.
        """
        response = self._http.request(
            "GET",
            "/users",
            params=_drop_none(
                {
                    "role": role,
                    "status": status,
                    "project_id": str(project_id) if project_id is not None else None,
                }
            ),
        )
        return [UserRecord.model_validate(item) for item in response.json()]

    def query(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        status: UserStatus = "active",
        project_id: IdLike | None = None,
        group_id: IdLike | None = None,
        role: str | None = None,
        search: str | None = None,
    ) -> UserPage:
        """Read a page using the API's project, group, status, role, and text filters."""
        response = self._http.request(
            "GET",
            "/users/query",
            params=_drop_none(
                {
                    "page": page,
                    "page_size": page_size,
                    "status": status,
                    "project_id": str(project_id) if project_id is not None else None,
                    "group_id": str(group_id) if group_id is not None else None,
                    "role": role,
                    "search": search,
                }
            ),
        )
        return UserPage.model_validate(response.json())

    def stats(
        self,
        *,
        status: UserStatus = "active",
        project_id: IdLike | None = None,
        group_id: IdLike | None = None,
        role: str | None = None,
        search: str | None = None,
    ) -> UsersStats:
        response = self._http.request(
            "GET",
            "/users/stats",
            params=_drop_none(
                {
                    "status": status,
                    "project_id": str(project_id) if project_id is not None else None,
                    "group_id": str(group_id) if group_id is not None else None,
                    "role": role,
                    "search": search,
                }
            ),
        )
        return UsersStats.model_validate(response.json())

    def export(
        self,
        dest: str | Path,
        *,
        format: Literal["csv", "json"] = "csv",
        status: UserStatus = "active",
        project_id: IdLike | None = None,
        group_id: IdLike | None = None,
        role: str | None = None,
        search: str | None = None,
    ) -> Path:
        """Stream the filtered user export atomically into an existing directory."""
        return self._http.stream_download(
            "/api/v1/users/export",
            Path(dest),
            params=_drop_none(
                {
                    "format": format,
                    "status": status,
                    "project_id": str(project_id) if project_id is not None else None,
                    "group_id": str(group_id) if group_id is not None else None,
                    "role": role,
                    "search": search,
                }
            ),
        )

    def invite(self, request: InvitationCreate) -> InvitationCreated:
        response = self._http.request(
            "POST",
            "/users/invite",
            json=request.model_dump(mode="json", exclude_unset=True, exclude_none=True),
        )
        return InvitationCreated.model_validate(response.json())

    def preview_bulk_invites(self, request: BulkInviteRequest) -> BulkInviteResponse:
        response = self._http.request(
            "POST",
            "/users/bulk-invite/preview",
            json=request.model_dump(mode="json", exclude_unset=True, exclude_none=True),
        )
        return BulkInviteResponse.model_validate(response.json())

    def bulk_invite(self, request: BulkInviteRequest) -> BulkInviteResponse:
        response = self._http.request(
            "POST",
            "/users/bulk-invite",
            json=request.model_dump(mode="json", exclude_unset=True, exclude_none=True),
        )
        return BulkInviteResponse.model_validate(response.json())

    def preview_bulk_group_assignment(
        self, request: BulkGroupAssignmentRequest
    ) -> BulkGroupAssignmentPreview:
        response = self._http.request(
            "POST",
            "/users/groups/bulk/preview",
            json=request.model_dump(mode="json", exclude_unset=True),
        )
        return BulkGroupAssignmentPreview.model_validate(response.json())

    def bulk_group_assignment(
        self, request: BulkGroupAssignmentRequest
    ) -> BulkGroupAssignmentResponse:
        response = self._http.request(
            "POST",
            "/users/groups/bulk",
            json=request.model_dump(mode="json", exclude_unset=True),
        )
        return BulkGroupAssignmentResponse.model_validate(response.json())

    def preview_role_change(
        self, user_id: IdLike, role: PlatformRole
    ) -> RoleImpactPreview:
        response = self._http.request(
            "GET",
            f"/users/{user_id}/role/preview",
            params={"role": role},
        )
        return RoleImpactPreview.model_validate(response.json())

    def change_role(self, user_id: IdLike, role: PlatformRole) -> UserRecord:
        response = self._http.request(
            "PATCH", f"/users/{user_id}/role", json={"role": role}
        )
        return UserRecord.model_validate(response.json())

    def assign_group(self, user_id: IdLike, group_id: IdLike | None) -> UserRecord:
        response = self._http.request(
            "PATCH",
            f"/users/{user_id}/group",
            json={"group_id": str(group_id) if group_id is not None else None},
        )
        return UserRecord.model_validate(response.json())

    def admin_reset_password(self, user_id: IdLike) -> AdminResetPasswordResponse:
        response = self._http.request("POST", f"/users/{user_id}/admin-reset-password")
        return AdminResetPasswordResponse.model_validate(response.json())

    def delete(
        self, user_id: IdLike, request: DeleteUserRequest | None = None
    ) -> UserRecord:
        response = self._http.request(
            "DELETE",
            f"/users/{user_id}",
            json=(
                request.model_dump(mode="json", exclude_unset=True, exclude_none=True)
                if request is not None
                else None
            ),
        )
        return UserRecord.model_validate(response.json())

    def deactivate(self, user_id: IdLike) -> UserRecord:
        response = self._http.request("POST", f"/users/{user_id}/deactivate")
        return UserRecord.model_validate(response.json())

    def offboarding_preview(self, user_id: IdLike) -> OffboardingPreview:
        response = self._http.request("GET", f"/users/{user_id}/offboarding-preview")
        return OffboardingPreview.model_validate(response.json())

    def offboard(
        self, user_id: IdLike, request: OffboardingCommitRequest
    ) -> OffboardingResult:
        response = self._http.request(
            "POST",
            f"/users/{user_id}/offboarding",
            json=request.model_dump(mode="json", exclude_unset=True, exclude_none=True),
        )
        return OffboardingResult.model_validate(response.json())

    def reactivate(
        self, user_id: IdLike, request: ReactivateRequest | None = None
    ) -> UserRecord:
        response = self._http.request(
            "POST",
            f"/users/{user_id}/reactivate",
            json=(
                request.model_dump(mode="json", exclude_unset=True, exclude_none=True)
                if request is not None
                else None
            ),
        )
        return UserRecord.model_validate(response.json())
