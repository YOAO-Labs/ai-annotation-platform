"""Administrator-only plugin installation resource."""

from __future__ import annotations

from uuid import UUID

from yoao._http import HttpTransport
from yoao.errors import APIStatusError
from yoao_inference_contract.plugin import (
    PluginActivateRequest,
    PluginInstallationResponse,
    PreparePluginInstallation,
    PluginInstallationCommand,
    PluginQuiesceResponse,
)
from yoao_inference_contract.plugin_assets import (
    PluginAssetApprovalState,
    PluginAssetApprovalUpdate,
)


class PluginInstallations:
    def __init__(self, http: HttpTransport):
        self._http = http

    def prepare(self, request: PreparePluginInstallation) -> PluginInstallationResponse:
        try:
            response = self._http.request(
                "POST",
                "/admin/ml-integrations/plugins/installations",
                json=request.model_dump(mode="json"),
            )
        except APIStatusError as exc:
            raise type(exc)(
                exc.status_code,
                "Plugin preparation failed",
                request_id=exc.request_id,
                retry_after=exc.retry_after,
            ) from None
        return PluginInstallationResponse.model_validate(response.json())

    def list(self) -> list[PluginInstallationResponse]:
        response = self._http.request(
            "GET", "/admin/ml-integrations/plugins/installations"
        )
        return [
            PluginInstallationResponse.model_validate(item) for item in response.json()
        ]

    def get(self, installation_id: str | UUID) -> PluginInstallationResponse:
        response = self._http.request(
            "GET", f"/admin/ml-integrations/plugins/installations/{installation_id}"
        )
        return PluginInstallationResponse.model_validate(response.json())

    def asset_approval(self, installation_id: str | UUID) -> PluginAssetApprovalState:
        response = self._http.request(
            "GET",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/assets",
        )
        return PluginAssetApprovalState.model_validate(response.json())

    def set_asset_approval(
        self, installation_id: str | UUID, request: PluginAssetApprovalUpdate
    ) -> PluginAssetApprovalState:
        response = self._http.request(
            "PUT",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/assets",
            json=request.model_dump(mode="json"),
        )
        return PluginAssetApprovalState.model_validate(response.json())

    def activate(
        self, installation_id: str | UUID, request: PluginActivateRequest
    ) -> PluginInstallationResponse:
        if UUID(str(installation_id)) != request.receipt.installation_id:
            raise ValueError("Installation ID must match launch receipt")
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/activate",
            json=request.model_dump(mode="json"),
            timeout=300,
        )
        return PluginInstallationResponse.model_validate(response.json())

    def disable(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginInstallationResponse:
        request = PluginInstallationCommand(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/disable",
            json=request.model_dump(mode="json"),
        )
        return PluginInstallationResponse.model_validate(response.json())

    def retire(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginInstallationResponse:
        request = PluginInstallationCommand(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/retire",
            json=request.model_dump(mode="json"),
        )
        return PluginInstallationResponse.model_validate(response.json())

    def cancel_prepared(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginInstallationResponse:
        request = PluginInstallationCommand(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/cancel-prepared",
            json=request.model_dump(mode="json"),
        )
        return PluginInstallationResponse.model_validate(response.json())

    def quiesce(
        self, installation_id: str | UUID, expected_revision: int
    ) -> PluginQuiesceResponse:
        request = PluginInstallationCommand(expected_revision=expected_revision)
        response = self._http.request(
            "POST",
            f"/admin/ml-integrations/plugins/installations/{installation_id}/quiesce",
            json=request.model_dump(mode="json"),
            timeout=300,
        )
        return PluginQuiesceResponse.model_validate(response.json())
