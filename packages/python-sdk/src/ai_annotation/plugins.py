"""Administrator-only plugin installation resource."""

from __future__ import annotations

from uuid import UUID

from ai_annotation._http import HttpTransport
from ai_annotation.errors import APIStatusError
from ai_annotation.plugin_models import (
    PluginActivateRequest,
    PluginInstallationResult,
    PluginPrepareRequest,
    PluginRevisionRequest,
    PluginQuiesceResult,
)


class PluginInstallations:
    def __init__(self, http: HttpTransport):
        self._http = http

    def prepare(self, request: PluginPrepareRequest) -> PluginInstallationResult:
        try:
            response = self._http.request(
                "POST",
                "/admin/ml-integrations/plugins/installations",
                json=request.model_dump(mode="json"),
            )
        except APIStatusError as exc:
            # Validation responses can echo the submitted write-only credential.
            raise type(exc)(
                exc.status_code,
                "Plugin preparation failed",
                request_id=exc.request_id,
                retry_after=exc.retry_after,
            ) from None
        return PluginInstallationResult.model_validate(response.json())

    def list(self) -> list[PluginInstallationResult]:
        response = self._http.request(
            "GET", "/admin/ml-integrations/plugins/installations"
        )
        return [
            PluginInstallationResult.model_validate(item) for item in response.json()
        ]

    def get(self, installation_id: str | UUID) -> PluginInstallationResult:
        response = self._http.request(
            "GET", f"/admin/ml-integrations/plugins/installations/{installation_id}"
        )
        return PluginInstallationResult.model_validate(response.json())

    def activate(
        self, installation_id: str | UUID, request: PluginActivateRequest
    ) -> PluginInstallationResult:
        if UUID(str(installation_id)) != request.receipt.installation_id:
            raise ValueError("Installation ID must match launch receipt")
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/activate",
            json=request.model_dump(mode="json"),
            timeout=300,
        )
        return PluginInstallationResult.model_validate(response.json())

    def disable(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginInstallationResult:
        request = PluginRevisionRequest(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/disable",
            json=request.model_dump(mode="json"),
        )
        return PluginInstallationResult.model_validate(response.json())

    def retire(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginInstallationResult:
        request = PluginRevisionRequest(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/retire",
            json=request.model_dump(mode="json"),
        )
        return PluginInstallationResult.model_validate(response.json())

    def cancel_prepared(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginInstallationResult:
        request = PluginRevisionRequest(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/cancel-prepared",
            json=request.model_dump(mode="json"),
        )
        return PluginInstallationResult.model_validate(response.json())

    def quiesce(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginQuiesceResult:
        request = PluginRevisionRequest(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/quiesce",
            json=request.model_dump(mode="json"),
            timeout=300,
        )
        return PluginQuiesceResult.model_validate(response.json())
