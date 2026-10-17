"""Client for global ML backend diagnostics and guarded runtime controls."""

from __future__ import annotations

from ai_annotation._http import HttpTransport
from ai_annotation.ml_diagnostic_models import (
    GPUArbiterResourcesResponse,
    MLIntegrationsOverview,
    ObserveResponse,
    ProbeRequest,
    ProbeResponse,
    RuntimeHints,
    SmokeTestRequest,
    SmokeTestResponse,
)


class MLDiagnostics:
    def __init__(self, http: HttpTransport):
        self._http = http

    def overview(self) -> MLIntegrationsOverview:
        """Summarize five object buckets and globally registered backend bindings."""
        response = self._http.request("GET", "/admin/ml-integrations/overview")
        return MLIntegrationsOverview.model_validate(response.json())

    def gpu_resources(self) -> GPUArbiterResourcesResponse:
        """Read static GPU claims and current arbiter runtime observations."""
        response = self._http.request("GET", "/admin/ml-integrations/gpu-resources")
        return GPUArbiterResourcesResponse.model_validate(response.json())

    def observe(self) -> ObserveResponse:
        """Probe configured environment backend URLs with /health and /setup."""
        response = self._http.request("GET", "/admin/ml-integrations/observe")
        return ObserveResponse.model_validate(response.json())

    def runtime_hints(self) -> RuntimeHints:
        response = self._http.request("GET", "/admin/ml-integrations/runtime-hints")
        return RuntimeHints.model_validate(response.json())

    def probe(self, body: ProbeRequest) -> ProbeResponse:
        """Probe a supplied backend /health URL; a failed probe is still an HTTP 200 result."""
        payload = body.model_dump(mode="json", exclude_unset=True)
        if body.auth_method == "none":
            payload.pop("auth_token", None)
        response = self._http.request(
            "POST",
            "/admin/ml-integrations/probe",
            json=payload,
        )
        return ProbeResponse.model_validate(response.json())

    def smoke_test(self, body: SmokeTestRequest) -> SmokeTestResponse:
        """Try loading a variant and request unload when the target pool is idle."""
        response = self._http.request(
            "POST",
            "/admin/ml-integrations/observe/smoke-test",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return SmokeTestResponse.model_validate(response.json())
