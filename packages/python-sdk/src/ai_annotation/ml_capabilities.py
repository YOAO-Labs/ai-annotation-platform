"""Authenticated protocol and backend instance capability discovery."""

from ai_annotation._http import HttpTransport
from ai_annotation.ml_capability_models import (
    CapabilityInstancesResponse,
    ProtocolCapabilitiesResponse,
)


class MLCapabilities:
    def __init__(self, http: HttpTransport):
        self._http = http

    def protocol(self) -> ProtocolCapabilitiesResponse:
        """Read current controlled vocabulary without conditional-cache headers."""
        response = self._http.request("GET", "/ml-capabilities/protocol")
        return ProtocolCapabilitiesResponse.model_validate(response.json())

    def instances(self) -> CapabilityInstancesResponse:
        """Read public instance models; the server can probe uncached backends."""
        response = self._http.request("GET", "/ml-capabilities/instances")
        return CapabilityInstancesResponse.model_validate(response.json())
