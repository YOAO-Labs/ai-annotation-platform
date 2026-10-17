"""Notification and per-account notification preference operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.notification_models import (
    NotificationActionResult,
    NotificationClearReadResult,
    NotificationMarkAllReadResult,
    NotificationPage,
    NotificationPreferenceUpdate,
    NotificationPreferences,
    NotificationUnreadCount,
)

IdLike = str | UUID


class Notifications:
    """Read and manage notifications belonging to the authenticated account."""

    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        unread_only: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> NotificationPage:
        """List notifications for the authenticated account."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        response = self._http.request(
            "GET",
            "/notifications",
            params={"unread_only": unread_only, "limit": limit, "offset": offset},
        )
        return NotificationPage.model_validate(response.json())

    def unread_count(self) -> NotificationUnreadCount:
        """Return the authenticated account's unread notification count."""
        response = self._http.request("GET", "/notifications/unread-count")
        return NotificationUnreadCount.model_validate(response.json())

    def mark_read(self, notification_id: IdLike) -> NotificationActionResult:
        """Mark one owned notification read; 404 means missing or already read."""
        response = self._http.request("POST", f"/notifications/{notification_id}/read")
        return NotificationActionResult.model_validate(response.json())

    def mark_all_read(self) -> NotificationMarkAllReadResult:
        """Mark all unread notifications for the authenticated account as read."""
        response = self._http.request("POST", "/notifications/mark-all-read")
        return NotificationMarkAllReadResult.model_validate(response.json())

    def delete(self, notification_id: IdLike) -> NotificationActionResult:
        """Delete one owned notification; a missing notification returns 404."""
        response = self._http.request("DELETE", f"/notifications/{notification_id}")
        return NotificationActionResult.model_validate(response.json())

    def clear_read(self) -> NotificationClearReadResult:
        """Delete all read notifications belonging to the authenticated account."""
        response = self._http.request("POST", "/notifications/clear-read")
        return NotificationClearReadResult.model_validate(response.json())

    def preferences(self) -> NotificationPreferences:
        """Get the server's effective preferences for known notification types."""
        response = self._http.request("GET", "/notification-preferences")
        return NotificationPreferences.model_validate(response.json())

    def update_preference(
        self, preference: NotificationPreferenceUpdate
    ) -> NotificationActionResult:
        """Patch only specified channels; the server owns the valid type list."""
        body: dict[str, Any] = preference.model_dump(
            mode="json", by_alias=True, exclude_unset=True
        )
        response = self._http.request("PUT", "/notification-preferences", json=body)
        return NotificationActionResult.model_validate(response.json())
