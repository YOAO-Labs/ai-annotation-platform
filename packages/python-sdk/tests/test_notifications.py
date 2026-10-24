from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest

from ai_annotation.errors import NotFoundError
from ai_annotation.notification_models import (
    Notification,
    NotificationClearReadResult,
    NotificationMarkAllReadResult,
    NotificationPage,
    NotificationPreferenceUpdate,
    NotificationPreferences,
    NotificationUnreadCount,
)
from ai_annotation.notifications import Notifications

from .conftest import API

NOTICE_ID = uuid4()
TARGET_ID = uuid4()


def _notification(**extra) -> dict:
    return {
        "id": str(NOTICE_ID),
        "type": "future_event_type",
        "target_type": "project",
        "target_id": str(TARGET_ID),
        "payload": {"project_name": "example", "future_key": {"n": 1}},
        "read_at": None,
        "created_at": "2026-10-17T00:00:00Z",
        **extra,
    }


def test_list_notifications_preserves_future_type_and_payload(client, respx_mock):
    route = respx_mock.get(f"{API}/notifications").mock(
        return_value=httpx.Response(
            200,
            json={"items": [_notification()], "total": 4, "unread": 2},
        )
    )

    page = Notifications(client._http).list(unread_only=True, limit=7, offset=3)

    assert isinstance(page, NotificationPage)
    assert page.items[0].type == "future_event_type"
    assert page.items[0].payload["future_key"] == {"n": 1}
    assert isinstance(page.items[0], Notification)
    assert (page.total, page.unread) == (4, 2)
    assert dict(route.calls.last.request.url.params) == {
        "unread_only": "true",
        "limit": "7",
        "offset": "3",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"nested": [{"count": 2**80, "label": "雪"}]},
        ["legacy", None, 2**100, False],
        "legacy scalar",
        0,
        False,
        None,
    ],
)
def test_list_notifications_preserves_native_payload_values(
    client, respx_mock, payload
):
    response = _notification(payload=payload)
    respx_mock.get(f"{API}/notifications").mock(
        return_value=httpx.Response(
            200,
            json={"items": [response], "total": 1, "unread": 1},
        )
    )

    page = Notifications(client._http).list()

    assert page.items[0].payload == payload


def test_notification_model_keeps_empty_object_default():
    notification = Notification(
        id=NOTICE_ID,
        type="new.notification",
        target_type="task",
        target_id=TARGET_ID,
        created_at=datetime.now(timezone.utc),
    )

    assert notification.payload == {}


@pytest.mark.parametrize(
    ("limit", "offset"),
    [(0, 0), (101, 0), (True, 0), (30, -1), (30, True)],
)
def test_invalid_list_bounds_do_not_send_http(client, respx_mock, limit, offset):
    with pytest.raises(ValueError):
        Notifications(client._http).list(limit=limit, offset=offset)
    assert not respx_mock.calls


def test_unread_count_and_preferences_use_response_envelopes(client, respx_mock):
    count = respx_mock.get(f"{API}/notifications/unread-count").mock(
        return_value=httpx.Response(200, json={"unread": 3})
    )
    preferences = respx_mock.get(f"{API}/notification-preferences").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "type": "future_event_type",
                        "in_app": False,
                        "email": True,
                        "toast": True,
                    }
                ]
            },
        )
    )
    client_notifications = Notifications(client._http)

    unread = client_notifications.unread_count()
    prefs = client_notifications.preferences()

    assert isinstance(unread, NotificationUnreadCount) and unread.unread == 3
    assert isinstance(prefs, NotificationPreferences)
    assert prefs.items[0].type == "future_event_type"
    assert prefs.items[0].email is True
    assert count.called and preferences.called


def test_notification_mutations_return_api_receipts(client, respx_mock):
    notifications = Notifications(client._http)
    read_route = respx_mock.post(f"{API}/notifications/{NOTICE_ID}/read").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    all_read_route = respx_mock.post(f"{API}/notifications/mark-all-read").mock(
        return_value=httpx.Response(200, json={"updated": 6})
    )
    delete_route = respx_mock.delete(f"{API}/notifications/{NOTICE_ID}").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    clear_route = respx_mock.post(f"{API}/notifications/clear-read").mock(
        return_value=httpx.Response(200, json={"deleted": 2})
    )

    assert notifications.mark_read(NOTICE_ID).ok is True
    all_read = notifications.mark_all_read()
    assert isinstance(all_read, NotificationMarkAllReadResult)
    assert all_read.updated == 6
    assert notifications.delete(NOTICE_ID).ok is True
    cleared = notifications.clear_read()
    assert isinstance(cleared, NotificationClearReadResult)
    assert cleared.deleted == 2
    assert all(
        route.called
        for route in (read_route, all_read_route, delete_route, clear_route)
    )


def test_mark_read_maps_404_for_missing_or_already_read(client, respx_mock):
    route = respx_mock.post(f"{API}/notifications/{NOTICE_ID}/read").mock(
        return_value=httpx.Response(
            404, json={"detail": "Notification not found or already read"}
        )
    )

    with pytest.raises(NotFoundError):
        Notifications(client._http).mark_read(NOTICE_ID)

    assert route.call_count == 1


def test_mutating_post_is_not_retried_on_network_failure(client, respx_mock):
    route = respx_mock.post(f"{API}/notifications/mark-all-read").mock(
        side_effect=httpx.ConnectError("connection dropped")
    )

    with pytest.raises(httpx.ConnectError):
        Notifications(client._http).mark_all_read()

    assert route.call_count == 1


def test_update_preference_sends_only_explicit_patch_fields(client, respx_mock):
    route = respx_mock.put(f"{API}/notification-preferences").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    body = NotificationPreferenceUpdate(type="future_event_type", toast=False)

    result = Notifications(client._http).update_preference(body)

    assert result.ok is True
    assert route.calls.last.request.content == (
        b'{"type":"future_event_type","toast":false}'
    )


@pytest.mark.parametrize(
    "data",
    [
        {"type": "task_assigned"},
        {"type": "task_assigned", "in_app": None},
        {"type": "task_assigned", "toast": None},
        {"type": "task_assigned", "in_app": True, "typo": False},
    ],
)
def test_empty_null_or_unknown_preference_patch_is_rejected(data):
    with pytest.raises(ValueError):
        NotificationPreferenceUpdate.model_validate(data)
