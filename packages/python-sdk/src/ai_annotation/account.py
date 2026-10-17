"""Authenticated self-service account operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ai_annotation._http import HttpTransport
from ai_annotation.errors import AAPError
from ai_annotation.models import Me
from ai_annotation.account_models import (
    AvatarRefUpdate,
    DeactivationRequest,
    PasswordChange,
    ProfileUpdate,
    TaskEventBatchRequest,
    TaskEventBatchResult,
)

MAX_AVATAR_FILE_BYTES = 2 * 1024 * 1024


def _body(request) -> dict[str, Any]:
    return request.model_dump(mode="json", by_alias=True, exclude_unset=True)


class Account:
    """Profile, preferences and self-service task events for the current user."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def update_profile(self, body: ProfileUpdate) -> Me:
        response = self._http.request("PATCH", "/auth/me", json=_body(body))
        return Me.model_validate(response.json())

    def get_preferences(self) -> dict[str, Any]:
        """Return the server preference snapshot, including opaque future values."""
        response = self._http.request("GET", "/auth/me/preferences")
        return response.json()

    def update_preferences(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Send only the caller's JSON patch; the server owns merge and CAS rules."""
        response = self._http.request("PATCH", "/auth/me/preferences", json=patch)
        return response.json()

    def change_password(self, body: PasswordChange) -> None:
        """Change the password without exposing either value through model reprs."""
        response = self._http.request(
            "POST",
            "/auth/me/password",
            json={
                "old_password": body.old_password.get_secret_value(),
                "new_password": body.new_password.get_secret_value(),
            },
        )
        if response.status_code != 204:
            raise AAPError("Password change did not return HTTP 204")

    def set_avatar_ref(self, body: AvatarRefUpdate) -> Me:
        """Select a preset or clear the avatar by passing ``avatar_ref=None``."""
        response = self._http.request(
            "PATCH",
            "/auth/me/avatar",
            json={"avatar_ref": body.avatar_ref},
        )
        return Me.model_validate(response.json())

    def clear_avatar(self) -> Me:
        response = self._http.request("DELETE", "/auth/me/avatar")
        return Me.model_validate(response.json())

    def upload_avatar(self, file_path: str | Path) -> Me:
        """Upload an image file as multipart without reading it all into memory."""
        path = Path(file_path)
        try:
            size = path.stat().st_size
        except OSError:
            raise AAPError("Unable to read avatar file") from None
        if size <= 0 or size > MAX_AVATAR_FILE_BYTES:
            raise AAPError("Avatar file must be between 1 byte and 2 MiB")
        try:
            file = path.open("rb")
        except OSError:
            raise AAPError("Unable to read avatar file") from None
        with file:
            response = self._http.request(
                "POST",
                "/auth/me/avatar",
                files={
                    "file": (path.name, file, "application/octet-stream"),
                },
            )
        return Me.model_validate(response.json())

    def request_deactivation(self, body: DeactivationRequest) -> Me:
        """Start the server's seven-day account deactivation cooling period."""
        response = self._http.request(
            "POST", "/auth/me/deactivation-request", json=_body(body)
        )
        return Me.model_validate(response.json())

    def cancel_deactivation(self) -> Me:
        response = self._http.request("DELETE", "/auth/me/deactivation-request")
        return Me.model_validate(response.json())

    def heartbeat(self) -> None:
        self._http.request("POST", "/auth/me/heartbeat")

    def submit_task_events(self, body: TaskEventBatchRequest) -> TaskEventBatchResult:
        """Submit closed intervals; supply stable client IDs for idempotent retries."""
        response = self._http.request(
            "POST",
            "/auth/me/task-events:batch",
            json=_body(body),
        )
        return TaskEventBatchResult.model_validate(response.json())
