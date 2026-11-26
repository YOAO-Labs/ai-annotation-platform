"""Super-admin trainer plugin registry resource."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from yoao._http import HttpTransport, _drop_none

TrainerPluginStatus = Literal[
    "registered", "qualified", "enabled", "disabled", "retired"
]
TrainerExecutionMode = Literal["container", "local_process"]
TrainerSourceTier = Literal["official", "third_party"]


class TrainerPluginRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: UUID
    plugin_id: str
    version: str
    extension_point: str = "trainer"
    framework_id: str
    display_name: str
    description: str | None = None
    homepage: str | None = None
    publisher: str | None = None
    license: str | None = None
    source_tier: TrainerSourceTier
    execution_mode: TrainerExecutionMode
    status: TrainerPluginStatus
    revision: int
    protocol_version: str
    artifact: str | None = None
    entrypoint: list[str]
    resource: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[str]
    manifest: dict[str, Any]
    capabilities_snapshot: dict[str, Any]
    capabilities_digest: str
    qualification_report_sha256: str | None = None
    qualification_report: dict[str, Any] | None = None
    qualified_at: datetime | None = None
    enabled_at: datetime | None = None
    disabled_at: datetime | None = None
    retired_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TrainerPlugins:
    def __init__(self, http: HttpTransport):
        self._http = http

    def list(
        self,
        *,
        include_retired: bool = False,
        status: TrainerPluginStatus | None = None,
    ) -> list[TrainerPluginRecord]:
        params = _drop_none(
            {
                "include_retired": include_retired if include_retired else None,
                "status": status,
            }
        )
        response = self._http.request(
            "GET",
            "/admin/trainer-plugins",
            params=params or None,
        )
        return [TrainerPluginRecord.model_validate(item) for item in response.json()]

    def get(self, plugin_id: str) -> TrainerPluginRecord:
        response = self._http.request("GET", f"/admin/trainer-plugins/{plugin_id}")
        return TrainerPluginRecord.model_validate(response.json())

    def register(
        self,
        manifest: dict[str, Any],
        capabilities_snapshot: dict[str, Any] | BaseModel,
        *,
        execution_mode: TrainerExecutionMode = "container",
        source_tier: TrainerSourceTier = "third_party",
        artifact: str | None = None,
    ) -> TrainerPluginRecord:
        snapshot = (
            capabilities_snapshot.model_dump(mode="json")
            if isinstance(capabilities_snapshot, BaseModel)
            else capabilities_snapshot
        )
        payload: dict[str, Any] = {
            "manifest": manifest,
            "capabilities_snapshot": snapshot,
            "execution_mode": execution_mode,
            "source_tier": source_tier,
            "artifact": artifact,
        }
        response = self._http.request("POST", "/admin/trainer-plugins", json=payload)
        return TrainerPluginRecord.model_validate(response.json())

    def qualify(
        self,
        plugin_id: str,
        *,
        expected_revision: int,
        qualification_report_sha256: str,
        qualification_report: dict[str, Any],
    ) -> TrainerPluginRecord:
        response = self._http.request(
            "POST",
            f"/admin/trainer-plugins/{plugin_id}/qualify",
            json={
                "expected_revision": expected_revision,
                "qualification_report_sha256": qualification_report_sha256,
                "qualification_report": qualification_report,
            },
        )
        return TrainerPluginRecord.model_validate(response.json())

    def enable(self, plugin_id: str, *, expected_revision: int) -> TrainerPluginRecord:
        response = self._http.request(
            "POST",
            f"/admin/trainer-plugins/{plugin_id}/enable",
            json={"expected_revision": expected_revision},
        )
        return TrainerPluginRecord.model_validate(response.json())

    def disable(self, plugin_id: str, *, expected_revision: int) -> TrainerPluginRecord:
        response = self._http.request(
            "POST",
            f"/admin/trainer-plugins/{plugin_id}/disable",
            json={"expected_revision": expected_revision},
        )
        return TrainerPluginRecord.model_validate(response.json())

    def retire(self, plugin_id: str, *, expected_revision: int) -> TrainerPluginRecord:
        response = self._http.request(
            "POST",
            f"/admin/trainer-plugins/{plugin_id}/retire",
            json={"expected_revision": expected_revision},
        )
        return TrainerPluginRecord.model_validate(response.json())
