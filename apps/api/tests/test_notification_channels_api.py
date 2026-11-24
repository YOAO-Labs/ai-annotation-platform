"""API and transactional enqueue tests for external notification channels."""

from __future__ import annotations

import uuid
from typing import Any

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import func, select, text

from app.api.v1 import notification_channels as channels_api
from app.config import settings
from app.db.models.notification import Notification
from app.services.notification import NotificationService
from yoao_notifications.channel_contract import (
    ChannelDeliveryOutcome,
    ChannelSetupResponse,
)
from yoao_notifications.channel_store_postgres import (
    NotificationChannelStorePostgresData,
)

_SCHEMA = {
    "type": "object",
    "properties": {"open_id": {"type": "string", "minLength": 2, "maxLength": 64}},
    "required": ["open_id"],
    "additionalProperties": False,
}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class _FakeChannelClient:
    def __init__(
        self,
        *,
        channel_id: str = "feishu",
        capabilities: tuple[str, ...] = ("deliver", "verify_address"),
        verify_outcome: ChannelDeliveryOutcome | None = None,
    ) -> None:
        self.channel_id = channel_id
        self.capabilities = capabilities
        self.verify_outcome = verify_outcome or ChannelDeliveryOutcome(
            outcome="delivered", provider_message_id="om_verify_1"
        )
        self.verify_calls: list[Any] = []

    async def health(self, *, endpoint_url: str) -> bool:
        return True

    async def setup(
        self, *, endpoint_url: str, auth_token: str
    ) -> ChannelSetupResponse:
        return ChannelSetupResponse(
            protocol_version="yoao-notification-channel/v1",
            channel_id=self.channel_id,
            display_name="Feishu DM",
            capabilities=self.capabilities,
            address_schema=_SCHEMA,
        )

    async def verify_address(
        self, *, endpoint_url: str, auth_token: str, request
    ) -> ChannelDeliveryOutcome:
        self.verify_calls.append(request)
        return self.verify_outcome


class _MemoryRedis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return 42

    async def aclose(self, close_connection_pool: bool = False) -> None:
        return None


@pytest.mark.asyncio
async def test_admin_routes_require_superadmin_and_fail_closed_without_key(
    httpx_client, annotator, super_admin, monkeypatch
):
    _, user_token = annotator
    _, admin_token = super_admin
    monkeypatch.setattr(settings, "notification_channel_encryption_key", "")
    monkeypatch.setattr(settings, "notification_channel_encryption_key_file", "")

    resp = await httpx_client.get(
        "/api/v1/admin/notification-channels", headers=_bearer(user_token)
    )
    assert resp.status_code == 403

    resp = await httpx_client.post(
        "/api/v1/admin/notification-channels",
        json={
            "endpoint_url": "https://feishu.example.com/plugin",
            "auth_token": "tok_abc",
        },
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_admin_register_ssrf_rejected_with_400(
    httpx_client, super_admin, monkeypatch
):
    _, admin_token = super_admin
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(settings, "notification_channel_encryption_key", key)
    monkeypatch.setattr(settings, "notification_channel_host_allowlist", [])
    monkeypatch.setattr(settings, "notification_channel_allow_insecure_http_hosts", [])

    resp = await httpx_client.post(
        "/api/v1/admin/notification-channels",
        json={
            "endpoint_url": "http://127.0.0.1:9999",
            "auth_token": "tok_abc",
        },
        headers=_bearer(admin_token),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_channel_lifecycle_user_binding_verify_and_preferences_v2(
    httpx_client, db_session, super_admin, annotator, monkeypatch
):
    _, admin_token = super_admin
    user, user_token = annotator
    key = Fernet.generate_key().decode("ascii")
    fake_client = _FakeChannelClient()
    fake_redis = _MemoryRedis()

    monkeypatch.setattr(settings, "notification_channel_encryption_key", key)
    monkeypatch.setattr(
        settings, "notification_channel_verify_rate_limit_per_minute", 2
    )
    monkeypatch.setattr(channels_api, "build_channel_http_client", lambda: fake_client)
    monkeypatch.setattr(channels_api, "_get_verify_redis", lambda: fake_redis)

    reg = await httpx_client.post(
        "/api/v1/admin/notification-channels",
        json={
            "endpoint_url": "https://feishu.example.com/plugin",
            "auth_token": "tok_super_secret",
            "description": "Internal Feishu bot",
            "homepage": "https://plugins.example.com/feishu",
        },
        headers=_bearer(admin_token),
    )
    assert reg.status_code == 201
    body = reg.json()
    assert body["channel_id"] == "feishu"
    assert body["source_tier"] == "third_party"
    assert body["status"] == "active"
    assert body["revision"] == 1
    assert "auth_token" not in body
    assert "auth_token_ciphertext" not in body

    dup = await httpx_client.post(
        "/api/v1/admin/notification-channels",
        json={
            "endpoint_url": "https://feishu.example.com/plugin",
            "auth_token": "tok_super_secret",
        },
        headers=_bearer(admin_token),
    )
    assert dup.status_code == 409

    probe = await httpx_client.post(
        "/api/v1/admin/notification-channels/feishu/probe",
        headers=_bearer(admin_token),
    )
    assert probe.status_code == 200
    assert probe.json()["healthy"] is True
    assert probe.json()["setup_ok"] is True

    user_list = await httpx_client.get(
        "/api/v1/notification-channels", headers=_bearer(user_token)
    )
    assert user_list.status_code == 200
    listed = user_list.json()["items"][0]
    assert listed["channel_id"] == "feishu"
    assert listed["bound"] is False
    assert "endpoint_url" not in listed
    assert "auth_token_sha256" not in listed

    bad_bind = await httpx_client.put(
        "/api/v1/notification-channels/feishu/binding",
        json={"address": {"open_id": "x"}, "locale": "zh-CN"},
        headers=_bearer(user_token),
    )
    assert bad_bind.status_code == 422

    ok_bind = await httpx_client.put(
        "/api/v1/notification-channels/feishu/binding",
        json={"address": {"open_id": "ou_123456"}, "locale": "zh-CN"},
        headers=_bearer(user_token),
    )
    assert ok_bind.status_code == 200
    assert ok_bind.json()["verified_at"] is None

    v1 = await httpx_client.post(
        "/api/v1/notification-channels/feishu/verify-address",
        headers=_bearer(user_token),
    )
    assert v1.status_code == 200
    assert v1.json()["verified_at"] is not None

    fake_client.verify_outcome = ChannelDeliveryOutcome(
        outcome="permanent", reason="open_id_not_found"
    )
    v2 = await httpx_client.post(
        "/api/v1/notification-channels/feishu/verify-address",
        headers=_bearer(user_token),
    )
    assert v2.status_code == 422

    v3 = await httpx_client.post(
        "/api/v1/notification-channels/feishu/verify-address",
        headers=_bearer(user_token),
    )
    assert v3.status_code == 429
    assert "Retry-After" in v3.headers

    bad_deact = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={
            "type": "user.deactivation_requested",
            "external_channels": {"feishu": True},
        },
        headers=_bearer(user_token),
    )
    assert bad_deact.status_code == 400

    bad_ch = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={"type": "task.rejected", "external_channels": {"unknown_ch": True}},
        headers=_bearer(user_token),
    )
    assert bad_ch.status_code == 400

    pref_ok = await httpx_client.put(
        "/api/v1/notification-preferences",
        json={
            "type": "task.rejected",
            "in_app": False,
            "external_channels": {"feishu": True},
        },
        headers=_bearer(user_token),
    )
    assert pref_ok.status_code == 200

    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    op_id = uuid.uuid4()
    row = await NotificationService(db_session).notify(
        user_id=user.id,
        type="task.rejected",
        target_type="task",
        target_id=task_id,
        payload={
            "task_display_id": "T-9",
            "project_id": str(project_id),
            "reject_reason_type": "missing_label",
            "reject_reason": "SECRET FREE TEXT MUST BE SCRUBBED",
            "actor_name": "Reviewer",
        },
        operation_id=op_id,
        project_id=project_id,
        task_id=task_id,
    )
    assert row is None
    notif_count = await db_session.scalar(
        select(func.count(Notification.id)).where(Notification.user_id == user.id)
    )
    assert notif_count == 0

    outbox = (
        await db_session.execute(
            text(
                "SELECT channel_id, notification_id, envelope_data "
                "FROM yoao_notifications_v1.notification_channel_deliveries "
                "WHERE recipient_id = :uid AND operation_id = :op_id"
            ),
            {"uid": user.id, "op_id": op_id},
        )
    ).all()
    assert len(outbox) == 1
    assert outbox[0][0] == "feishu"
    assert outbox[0][1] is None
    assert "reject_reason" not in outbox[0][2]
    assert outbox[0][2]["reject_reason_type"] == "missing_label"
    assert outbox[0][2]["action_url"] == f"/projects/{project_id}/workbench/{task_id}"

    unbind = await httpx_client.delete(
        "/api/v1/notification-channels/feishu/binding",
        headers=_bearer(user_token),
    )
    assert unbind.status_code == 200
    assert unbind.json()["deleted"] is True

    stale_disable = await httpx_client.post(
        "/api/v1/admin/notification-channels/feishu/disable",
        json={"expected_revision": 99},
        headers=_bearer(admin_token),
    )
    assert stale_disable.status_code == 409

    disabled = await httpx_client.post(
        "/api/v1/admin/notification-channels/feishu/disable",
        json={"expected_revision": 1},
        headers=_bearer(admin_token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert disabled.json()["revision"] == 2

    retired = await httpx_client.post(
        "/api/v1/admin/notification-channels/feishu/retire",
        json={"expected_revision": 2},
        headers=_bearer(admin_token),
    )
    assert retired.status_code == 200
    assert retired.json()["status"] == "retired"
    assert (
        await NotificationChannelStorePostgresData(db_session).open_channel_secret(
            "feishu"
        )
        is None
    )


@pytest.mark.asyncio
async def test_manifest_registration_null_patch_and_prepared_channel_hiding(
    httpx_client, db_session, super_admin, annotator, reviewer, monkeypatch
):
    admin_user, admin_token = super_admin
    user, user_token = annotator
    other, _ = reviewer
    key = Fernet.generate_key().decode("ascii")
    fake_client = _FakeChannelClient(channel_id="wecom")
    monkeypatch.setattr(settings, "notification_channel_encryption_key", key)
    monkeypatch.setattr(channels_api, "build_channel_http_client", lambda: fake_client)

    manifest = {
        "manifest_schema": "yoao-extension-manifest/v1",
        "extension_point": "notification_channel",
        "plugin_id": "wecom-dm",
        "version": "1.0.0",
        "display_name": "WeCom Manifest Name",
        "description": "Manifest description",
        "homepage": "https://plugins.example.com/wecom",
        "publisher": "yoao",
        "license": "Apache-2.0",
        "protocol_version": "yoao-notification-channel/v1",
        "entrypoint": ["wecom-bot", "--port", "9000"],
        "capabilities": ["deliver", "verify_address"],
        "resource": {"memory_mb": 256},
        "extension": {
            "channel_id": "wecom",
            "address_schema": _SCHEMA,
            "allowed_egress_hosts": ["qyapi.weixin.qq.com"],
        },
    }
    bad_manifest = {
        **manifest,
        "extension": {**manifest["extension"], "channel_id": "feishu"},
    }
    mismatch = await httpx_client.post(
        "/api/v1/admin/notification-channels",
        json={
            "endpoint_url": "https://wecom.example.com/plugin",
            "auth_token": "tok_wecom",
            "mode": "managed",
            "status": "prepared",
            "manifest": bad_manifest,
        },
        headers=_bearer(admin_token),
    )
    assert mismatch.status_code == 422

    reg = await httpx_client.post(
        "/api/v1/admin/notification-channels",
        json={
            "endpoint_url": "https://wecom.example.com/plugin",
            "auth_token": "tok_wecom",
            "mode": "managed",
            "status": "prepared",
            "manifest": manifest,
        },
        headers=_bearer(admin_token),
    )
    assert reg.status_code == 201
    created = reg.json()
    assert created["display_name"] == "WeCom Manifest Name"
    assert created["description"] == "Manifest description"
    assert created["status"] == "prepared"

    bind_prepared = await httpx_client.put(
        "/api/v1/notification-channels/wecom/binding",
        json={"address": {"open_id": "ou_123"}, "locale": "zh-CN"},
        headers=_bearer(user_token),
    )
    assert bind_prepared.status_code == 404

    verify_prepared = await httpx_client.post(
        "/api/v1/notification-channels/wecom/verify-address",
        headers=_bearer(user_token),
    )
    assert verify_prepared.status_code == 404

    null_required = await httpx_client.patch(
        "/api/v1/admin/notification-channels/wecom",
        json={"expected_revision": 1, "display_name": None},
        headers=_bearer(admin_token),
    )
    assert null_required.status_code == 422

    clear_desc = await httpx_client.patch(
        "/api/v1/admin/notification-channels/wecom",
        json={"expected_revision": 1, "description": None},
        headers=_bearer(admin_token),
    )
    assert clear_desc.status_code == 200
    assert clear_desc.json()["description"] is None

    project_id = uuid.uuid4()
    batch_id = uuid.uuid4()
    task_id = uuid.uuid4()
    await db_session.execute(
        text(
            "INSERT INTO public.projects (id, display_id, name, type_label, type_key, data_type, owner_id, status) "
            "VALUES (:pid, :disp, 'VisProj', '2D', 'detection_2d', 'image', :owner, 'in_progress')"
        ),
        {"pid": project_id, "disp": f"P-{project_id.hex[:6]}", "owner": admin_user.id},
    )
    await db_session.execute(
        text(
            "INSERT INTO public.project_members (id, project_id, user_id, role) VALUES "
            "(:id1, :pid, :u1, 'annotator'), (:id2, :pid, :u2, 'annotator')"
        ),
        {
            "id1": uuid.uuid4(),
            "id2": uuid.uuid4(),
            "pid": project_id,
            "u1": user.id,
            "u2": other.id,
        },
    )
    await db_session.execute(
        text(
            "INSERT INTO public.task_batches (id, project_id, display_id, name, status, annotator_id) "
            "VALUES (:bid, :pid, :bdisp, 'B-1', 'annotating', :other)"
        ),
        {
            "bid": batch_id,
            "pid": project_id,
            "bdisp": f"B-{batch_id.hex[:6]}",
            "other": other.id,
        },
    )
    await db_session.execute(
        text(
            "INSERT INTO public.tasks (id, display_id, project_id, batch_id, file_name, file_path, file_type, status, assignee_id) "
            "VALUES (:tid, :disp, :pid, :bid, 'a.jpg', 's3://b/a.jpg', 'image', 'in_progress', NULL)"
        ),
        {
            "tid": task_id,
            "disp": f"T-{task_id.hex[:6]}",
            "pid": project_id,
            "bid": batch_id,
        },
    )
    visible_other = await db_session.scalar(
        text(
            "SELECT yoao_labeling_v1.notification_target_visible_to_actor("
            ":actor, 'task', :tid, :pid, :tid)"
        ),
        {"actor": other.id, "tid": task_id, "pid": project_id},
    )
    visible_user = await db_session.scalar(
        text(
            "SELECT yoao_labeling_v1.notification_target_visible_to_actor("
            ":actor, 'task', :tid, :pid, :tid)"
        ),
        {"actor": user.id, "tid": task_id, "pid": project_id},
    )
    assert visible_other is True
    assert visible_user is False
