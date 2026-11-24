"""Notification, preference, and external notification channel operations."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from yoao._http import HttpTransport
from yoao.notification_models import (
    DeleteChannelBindingResult,
    NotificationActionResult,
    NotificationChannelAdmin,
    NotificationChannelAdminList,
    NotificationChannelProbeResult,
    NotificationChannelRegisterRequest,
    NotificationChannelUpdateRequest,
    NotificationClearReadResult,
    NotificationMarkAllReadResult,
    NotificationPage,
    NotificationPreferenceUpdate,
    NotificationPreferences,
    NotificationUnreadCount,
    UserChannelBindingUpsertRequest,
    UserNotificationChannelBinding,
    UserNotificationChannelList,
    VerifyChannelAddressResult,
)

IdLike = str | UUID


class Notifications:
    """Read and manage notifications, preferences, and external notification channels."""

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

    def list_channels(self) -> UserNotificationChannelList:
        """List active and disabled external notification channels with current bindings."""
        response = self._http.request("GET", "/notification-channels")
        return UserNotificationChannelList.model_validate(response.json())

    def upsert_channel_binding(
        self, channel_id: str, binding: UserChannelBindingUpsertRequest
    ) -> UserNotificationChannelBinding:
        """Bind or update the authenticated account's recipient address for a channel."""
        response = self._http.request(
            "PUT",
            f"/notification-channels/{channel_id}/binding",
            json=binding.model_dump(mode="json"),
        )
        return UserNotificationChannelBinding.model_validate(response.json())

    def verify_channel_address(self, channel_id: str) -> VerifyChannelAddressResult:
        """Trigger a channel plugin test message to verify the bound recipient address."""
        response = self._http.request(
            "POST", f"/notification-channels/{channel_id}/verify-address"
        )
        return VerifyChannelAddressResult.model_validate(response.json())

    def delete_channel_binding(self, channel_id: str) -> DeleteChannelBindingResult:
        """Delete the authenticated account's binding for an external channel."""
        response = self._http.request(
            "DELETE", f"/notification-channels/{channel_id}/binding"
        )
        return DeleteChannelBindingResult.model_validate(response.json())

    def list_admin_channels(
        self, *, include_retired: bool = False
    ) -> NotificationChannelAdminList:
        """List registered external notification channels (super_admin)."""
        response = self._http.request(
            "GET",
            "/admin/notification-channels",
            params={"include_retired": include_retired},
        )
        return NotificationChannelAdminList.model_validate(response.json())

    def register_admin_channel(
        self, channel: NotificationChannelRegisterRequest
    ) -> NotificationChannelAdmin:
        """Register an external notification channel after live /setup verification."""
        response = self._http.request(
            "POST",
            "/admin/notification-channels",
            json=channel.model_dump(mode="json", exclude_none=True),
        )
        return NotificationChannelAdmin.model_validate(response.json())

    def get_admin_channel(self, channel_id: str) -> NotificationChannelAdmin:
        """Read one external notification channel record (super_admin)."""
        response = self._http.request(
            "GET", f"/admin/notification-channels/{channel_id}"
        )
        return NotificationChannelAdmin.model_validate(response.json())

    def probe_admin_channel(self, channel_id: str) -> NotificationChannelProbeResult:
        """Run live /health and /setup probes for a registered notification channel."""
        response = self._http.request(
            "POST", f"/admin/notification-channels/{channel_id}/probe"
        )
        return NotificationChannelProbeResult.model_validate(response.json())

    def update_admin_channel(
        self, channel_id: str, update: NotificationChannelUpdateRequest
    ) -> NotificationChannelAdmin:
        """Update an external notification channel with CAS expected_revision."""
        response = self._http.request(
            "PATCH",
            f"/admin/notification-channels/{channel_id}",
            json=update.model_dump(mode="json", exclude_unset=True),
        )
        return NotificationChannelAdmin.model_validate(response.json())

    def enable_admin_channel(
        self, channel_id: str, *, expected_revision: int
    ) -> NotificationChannelAdmin:
        """Activate a prepared or disabled notification channel with CAS revision."""
        response = self._http.request(
            "POST",
            f"/admin/notification-channels/{channel_id}/enable",
            json={"expected_revision": expected_revision},
        )
        return NotificationChannelAdmin.model_validate(response.json())

    def disable_admin_channel(
        self, channel_id: str, *, expected_revision: int
    ) -> NotificationChannelAdmin:
        """Disable an active notification channel with CAS revision."""
        response = self._http.request(
            "POST",
            f"/admin/notification-channels/{channel_id}/disable",
            json={"expected_revision": expected_revision},
        )
        return NotificationChannelAdmin.model_validate(response.json())

    def retire_admin_channel(
        self, channel_id: str, *, expected_revision: int
    ) -> NotificationChannelAdmin:
        """Retire a prepared or disabled notification channel and wipe credentials."""
        response = self._http.request(
            "POST",
            f"/admin/notification-channels/{channel_id}/retire",
            json={"expected_revision": expected_revision},
        )
        return NotificationChannelAdmin.model_validate(response.json())
