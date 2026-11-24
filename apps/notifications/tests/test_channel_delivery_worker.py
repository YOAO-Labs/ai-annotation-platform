"""Unit tests for NotificationChannelDeliveryWorker pre-delivery guards and outcomes."""

from __future__ import annotations

import uuid
from typing import Any

from cryptography.fernet import Fernet
import pytest

from yoao_notifications.channel_contract import ChannelDeliveryOutcome
from yoao_notifications.channel_crypto import encrypt_channel_token
from yoao_notifications.channel_delivery_postgres import (
    _CHANNEL_DELIVERY_CAPABILITIES,
    CHANNEL_DELIVERY_POLICY,
)
from yoao_notifications.channel_delivery_worker import NotificationChannelDeliveryWorker

_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"open_id": {"type": "string", "minLength": 1, "maxLength": 64}},
    "required": ["open_id"],
    "additionalProperties": False,
}


class _FakeData:
    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        bug_visible: bool = True,
        labeling_visible: bool = True,
        job_scopes: list[tuple[uuid.UUID, uuid.UUID | None, str]] | None = None,
        owner_pairs: set[tuple[uuid.UUID, uuid.UUID]] | None = None,
        visibility_error: Exception | None = None,
    ) -> None:
        self.items = items
        self.bug_visible = bug_visible
        self.labeling_visible = labeling_visible
        self._job_scopes = job_scopes or []
        self._owner_pairs = owner_pairs or set()
        self.visibility_error = visibility_error
        self.finished: list[dict[str, Any]] = []

    async def channel_delivery_policy(self) -> str:
        return CHANNEL_DELIVERY_POLICY

    async def claim_deliveries(self, worker_id, *, limit=16, lease_seconds=60):
        return self.items

    async def finish_delivery(self, **kwargs) -> bool:
        self.finished.append(kwargs)
        return True

    async def is_bug_report_visible(self, actor_id, report_id) -> bool:
        if self.visibility_error is not None:
            raise self.visibility_error
        return self.bug_visible

    async def is_labeling_target_visible(
        self, actor_id, target_type, target_id, project_id, task_id
    ) -> bool:
        if self.visibility_error is not None:
            raise self.visibility_error
        return self.labeling_visible

    async def job_scopes(self, job_ids):
        if self.visibility_error is not None:
            raise self.visibility_error
        return self._job_scopes

    async def member_pairs(self, pairs):
        return set()

    async def reviewer_pairs(self, pairs):
        return set()

    async def owner_pairs(self, pairs):
        return self._owner_pairs

    async def super_pairs(self, pairs):
        return set()

    async def global_super_admins(self, user_ids):
        return set()


class _FakeHttpClient:
    def __init__(self, outcome: ChannelDeliveryOutcome) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def deliver(self, *, endpoint_url, auth_token, request):
        self.calls.append(
            {
                "endpoint_url": endpoint_url,
                "auth_token": auth_token,
                "request": request,
            }
        )
        return self.outcome


def _make_item(key: str, **overrides: Any) -> dict[str, Any]:
    task_id = uuid.uuid4()
    project_id = uuid.uuid4()
    base: dict[str, Any] = {
        "delivery_id": str(uuid.uuid4()),
        "operation_id": str(uuid.uuid4()),
        "notification_id": str(uuid.uuid4()),
        "channel_id": "feishu",
        "recipient_id": str(uuid.uuid4()),
        "notification_type": "task.rejected",
        "target_type": "task",
        "target_id": str(task_id),
        "project_id": str(project_id),
        "task_id": str(task_id),
        "envelope_data": {
            "target_type": "task",
            "target_id": str(task_id),
            "project_id": str(project_id),
            "task_id": str(task_id),
            "action_url": f"/projects/{project_id}/workbench/{task_id}",
        },
        "attempt": 1,
        "max_attempts": 6,
        "worker_id": str(uuid.uuid4()),
        "lease_id": str(uuid.uuid4()),
        "occurred_at": "2026-11-24T08:00:00.000000Z",
        "channel_status": "active",
        "endpoint_url": "https://feishu.example.com/hook",
        "auth_token_ciphertext": encrypt_channel_token("tok_secret_1", key),
        "address_schema": _ADDRESS_SCHEMA,
        "account_active": True,
        "preference_enabled": True,
        "recipient_address": {"open_id": "ou_12345"},
        "locale": "zh-CN",
    }
    base.update(overrides)
    return base


def test_channel_delivery_capabilities_exact_count() -> None:
    assert len(_CHANNEL_DELIVERY_CAPABILITIES) == 10
    assert (
        "yoao_notifications_v1.claim_channel_deliveries(jsonb)"
        in _CHANNEL_DELIVERY_CAPABILITIES
    )
    assert (
        "yoao_diagnostics_v1.bug_report_visible_to_actor(uuid, uuid)"
        in _CHANNEL_DELIVERY_CAPABILITIES
    )


@pytest.mark.asyncio
async def test_worker_delivers_valid_task_event_with_five_field_envelope() -> None:
    key = Fernet.generate_key().decode("ascii")
    item = _make_item(key)
    data = _FakeData([item])
    client = _FakeHttpClient(
        ChannelDeliveryOutcome(outcome="delivered", provider_message_id="om_999")
    )
    worker = NotificationChannelDeliveryWorker(data, client, encryption_key=key)

    counts = await worker.run_once()

    assert counts["claimed"] == 1
    assert counts["delivered"] == 1
    assert len(client.calls) == 1
    sent = client.calls[0]
    assert sent["auth_token"] == "tok_secret_1"
    dumped = sent["request"].model_dump(mode="json")
    assert set(dumped["envelope"].keys()) == {
        "event_version",
        "event",
        "delivery_id",
        "occurred_at",
        "data",
    }
    assert dumped["envelope"]["delivery_id"] == item["delivery_id"]
    assert data.finished[0]["outcome"] == "delivered"
    assert data.finished[0]["provider_message_id"] == "om_999"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "expected_outcome", "expected_reason"),
    [
        ({"channel_status": "disabled"}, "skipped", "channel_disabled"),
        ({"channel_status": "retired"}, "skipped", "channel_disabled"),
        ({"account_active": False}, "skipped", "access_revoked"),
        ({"preference_enabled": False}, "skipped", "preference_disabled"),
        ({"recipient_address": None}, "skipped", "binding_removed"),
        (
            {"recipient_address": {"wrong": "x"}},
            "permanent",
            "invalid_recipient_address",
        ),
    ],
)
async def test_worker_pre_delivery_guards_skip_without_http(
    overrides: dict[str, Any], expected_outcome: str, expected_reason: str
) -> None:
    key = Fernet.generate_key().decode("ascii")
    item = _make_item(key, **overrides)
    data = _FakeData([item])
    client = _FakeHttpClient(ChannelDeliveryOutcome(outcome="delivered"))
    worker = NotificationChannelDeliveryWorker(data, client, encryption_key=key)

    await worker.run_once()

    assert client.calls == []
    assert data.finished[0]["outcome"] == expected_outcome
    assert data.finished[0]["reason"] == expected_reason


@pytest.mark.asyncio
async def test_worker_skips_revoked_bug_report_and_task_without_http() -> None:
    key = Fernet.generate_key().decode("ascii")
    bug_item = _make_item(
        key,
        notification_type="bug_report.commented",
        target_type="bug_report",
        target_id=str(uuid.uuid4()),
        project_id=None,
        task_id=None,
    )
    task_item = _make_item(key)
    data = _FakeData([bug_item, task_item], bug_visible=False, labeling_visible=False)
    client = _FakeHttpClient(ChannelDeliveryOutcome(outcome="delivered"))
    worker = NotificationChannelDeliveryWorker(data, client, encryption_key=key)

    counts = await worker.run_once()

    assert counts["skipped"] == 2
    assert client.calls == []
    assert all(row["reason"] == "access_revoked" for row in data.finished)


@pytest.mark.asyncio
async def test_worker_never_fails_open_on_visibility_error_or_private_migration() -> (
    None
):
    key = Fernet.generate_key().decode("ascii")
    bug_item = _make_item(
        key,
        notification_type="bug_report.commented",
        target_type="bug_report",
        target_id=str(uuid.uuid4()),
    )
    data = _FakeData([bug_item], visibility_error=RuntimeError("db down"))
    client = _FakeHttpClient(ChannelDeliveryOutcome(outcome="delivered"))
    worker = NotificationChannelDeliveryWorker(data, client, encryption_key=key)

    await worker.run_once()
    assert client.calls == []
    assert data.finished[0]["outcome"] == "retryable"
    assert data.finished[0]["reason"] == "visibility_check_unavailable"

    migration_job_id = uuid.uuid4()
    mig_item = _make_item(
        key,
        notification_type="job.completed",
        target_type="async_job",
        target_id=str(migration_job_id),
        project_id=None,
        task_id=None,
        envelope_data={"kind": "storage_migration_copy", "status": "completed"},
    )
    data2 = _FakeData(
        [mig_item],
        job_scopes=[(migration_job_id, None, "storage_migration_copy")],
    )
    worker2 = NotificationChannelDeliveryWorker(data2, client, encryption_key=key)
    await worker2.run_once()
    assert client.calls == []
    assert data2.finished[0]["outcome"] == "skipped"
    assert data2.finished[0]["reason"] == "access_revoked"


@pytest.mark.asyncio
async def test_worker_checks_export_authorization_and_key_failure() -> None:
    key = Fernet.generate_key().decode("ascii")
    job_id = uuid.uuid4()
    project_id = uuid.uuid4()
    recipient_id = uuid.uuid4()
    export_item = _make_item(
        key,
        recipient_id=str(recipient_id),
        notification_type="export.ready",
        target_type="export",
        target_id=str(job_id),
        project_id=str(project_id),
        task_id=None,
        envelope_data={
            "target_type": "export",
            "target_id": str(job_id),
            "project_id": str(project_id),
        },
    )
    denied_data = _FakeData(
        [export_item], job_scopes=[(job_id, project_id, "export")], owner_pairs=set()
    )
    client = _FakeHttpClient(ChannelDeliveryOutcome(outcome="delivered"))
    await NotificationChannelDeliveryWorker(
        denied_data, client, encryption_key=key
    ).run_once()
    assert client.calls == []
    assert denied_data.finished[0]["outcome"] == "skipped"
    assert denied_data.finished[0]["reason"] == "access_revoked"

    allowed_data = _FakeData(
        [export_item],
        job_scopes=[(job_id, project_id, "export")],
        owner_pairs={(project_id, recipient_id)},
    )
    await NotificationChannelDeliveryWorker(
        allowed_data, client, encryption_key=""
    ).run_once()
    assert client.calls == []
    assert allowed_data.finished[0]["outcome"] == "retryable"
    assert allowed_data.finished[0]["reason"] == "channel_key_unavailable"
