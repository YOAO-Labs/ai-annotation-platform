"""Operational DTOs preserve wire preconditions and one-shot side effects."""

import json

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from ai_annotation import (
    AuditExportQuery,
    AuditLogQuery,
    SystemSettingsReset,
    SystemSettingsUpdate,
)
from ai_annotation.cli.main import app
from ai_annotation.errors import APIStatusError

from .conftest import API


def _settings():
    return {
        "environment": "test",
        "invitation_ttl_days": 7,
        "frontend_base_url": "http://testserver",
        "smtp": {
            "host": None,
            "port": None,
            "user": None,
            "from_address": None,
            "configured": False,
        },
        "allow_open_registration": False,
        "max_invitations_per_day": 10,
        "offline_threshold_minutes": 5,
        "dataset_import_max_files": 100,
        "dataset_import_max_total_bytes": 1000,
        "task_create_sync_threshold": 10,
        "video_chunk_warmup_lookahead": 3,
        "version": "settings-r1",
        "metadata": {},
    }


def test_system_settings_preserve_version_null_and_password_clear(client, respx_mock):
    respx_mock.get(f"{API}/settings/system").mock(
        return_value=httpx.Response(200, json=_settings())
    )
    assert client.system_settings.get().version == "settings-r1"
    patch = respx_mock.patch(f"{API}/settings/system").mock(
        return_value=httpx.Response(200, json=_settings())
    )
    client.system_settings.update(
        SystemSettingsUpdate(
            expected_version="settings-r1", smtp_password="", smtp_host=None
        )
    )
    assert json.loads(patch.calls.last.request.content) == {
        "expected_version": "settings-r1",
        "smtp_password": "",
        "smtp_host": None,
    }
    reset = respx_mock.post(f"{API}/settings/system/reset").mock(
        return_value=httpx.Response(200, json=_settings())
    )
    client.system_settings.reset(
        SystemSettingsReset(keys=["smtp_host"], expected_version="settings-r1")
    )
    assert json.loads(reset.calls.last.request.content) == {
        "keys": ["smtp_host"],
        "expected_version": "settings-r1",
    }
    smtp = respx_mock.post(f"{API}/settings/system/test-smtp").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "to": "admin@example.com",
                "from": "bot@example.com",
                "host": "mail.example.com",
                "port": 587,
            },
        )
    )
    assert client.system_settings.test_smtp().from_address == "bot@example.com"
    assert smtp.call_count == 1
    with pytest.raises(ValidationError):
        SystemSettingsReset(keys=["smtp_host", "smtp_host"])
    with pytest.raises(ValidationError):
        SystemSettingsUpdate(smtp_port=True)
    assert "private-smtp" not in repr(
        SystemSettingsUpdate(smtp_password="private-smtp")
    )


def test_smtp_cli_refusal_sends_no_email(monkeypatch, respx_mock):
    monkeypatch.setenv("AAP_BASE_URL", "http://testserver")
    monkeypatch.setenv("AAP_API_KEY", "ak_test")
    route = respx_mock.post(f"{API}/settings/system/test-smtp")
    result = CliRunner().invoke(
        app, ["--error-format", "json", "system-settings", "test-smtp", "--json"]
    )
    assert result.exit_code != 0 and not route.called
    assert json.loads(result.stderr)["kind"] == "usage_error"


def test_capabilities_return_typed_instances_without_conditional_headers(
    client, respx_mock
):
    protocol = respx_mock.get(f"{API}/ml-capabilities/protocol").mock(
        return_value=httpx.Response(
            200,
            json={
                "version": "2",
                "tasks": [],
                "infras": [],
                "modalities": [],
                "geometries": [],
            },
        )
    )
    instances = respx_mock.get(f"{API}/ml-capabilities/instances").mock(
        return_value=httpx.Response(
            200,
            json={
                "instances": [
                    {
                        "backend_id": "registry-a",
                        "state": "ready",
                        "source": "registry",
                        "name": "Model server",
                        "models": [{"id": "model-a", "display_name": "Model A"}],
                    }
                ]
            },
        )
    )
    assert client.ml_capabilities.protocol().version == "2"
    assert client.ml_capabilities.instances().instances[0].models[0].id == "model-a"
    assert "if-none-match" not in protocol.calls.last.request.headers
    assert "if-none-match" not in instances.calls.last.request.headers


def test_audit_queries_aliases_archive_and_export_atomicity(
    client, respx_mock, tmp_path
):
    logs = respx_mock.get(f"{API}/audit-logs").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [],
                "total": 0,
                "page": 1,
                "page_size": 2,
                "next_cursor": None,
            },
        )
    )
    client.audit_logs.list(
        AuditLogQuery(from_="2026-10-17T00:00:00Z", cursor="opaque", page_size=2)
    )
    assert logs.calls.last.request.url.params["from"].startswith("2026-10-17")
    assert logs.calls.last.request.url.params["cursor"] == "opaque"
    assert "from_" not in logs.calls.last.request.url.params
    respx_mock.get(f"{API}/audit-logs/monthly-summary").mock(
        return_value=httpx.Response(
            200,
            json={
                "month": "2026-10",
                "materialized_through": None,
                "totals": {"event_count": 0, "error_count": 0, "action_kind_count": 0},
                "daily": [],
                "top_actions": [],
                "target_types": [],
                "actor_roles": [],
            },
        )
    )
    assert client.audit_logs.monthly_summary("2026-10").totals.event_count == 0
    respx_mock.get(f"{API}/audit-logs/archives").mock(
        return_value=httpx.Response(
            200, json=[{"year": 2026, "month": 9, "object_key": "audit/2026-09.jsonl"}]
        )
    )
    assert client.audit_logs.archives()[0].month == 9
    archive = respx_mock.get(f"{API}/audit-logs/archives/2026/9").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [{"action": "task.created"}],
                "total": 1,
                "limit": 1,
                "offset": 2,
                "year": 2026,
                "month": 9,
            },
        )
    )
    assert (
        client.audit_logs.archive(2026, 9, limit=1, offset=2).items[0]["action"]
        == "task.created"
    )
    assert archive.calls.last.request.url.params["offset"] == "2"
    export = respx_mock.get(f"{API}/audit-logs/export").mock(
        return_value=httpx.Response(200, content=b'{"audit_logs":[]}')
    )
    dest = tmp_path / "audit.json"
    assert client.audit_logs.export(dest, AuditExportQuery(format="json")) == dest
    assert dest.read_bytes() == b'{"audit_logs":[]}'
    export.mock(return_value=httpx.Response(503, json={"detail": "unavailable"}))
    with pytest.raises(APIStatusError) as failure:
        client.audit_logs.export(dest)
    assert failure.value.status_code == 503
    assert export.call_count == 2 and dest.read_bytes() == b'{"audit_logs":[]}'
    with pytest.raises(ValidationError):
        client.audit_logs.archive(2026, 13)
    with pytest.raises(ValidationError):
        AuditLogQuery(detail_key="project_id")
    assert AuditLogQuery(detail_key="reason", detail_value="").detail_value == ""
