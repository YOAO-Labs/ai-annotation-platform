"""Same-origin health endpoints outside the versioned API prefix."""

from ai_annotation._http import HttpTransport
from ai_annotation.health_models import HealthCheck, HealthReport


class Health:
    def __init__(self, http: HttpTransport):
        self._http = http

    def get(self) -> HealthReport:
        """Return overall readiness; HTTP 503 is a degraded JSON result."""
        response = self._http.request(
            "GET", "/health", root_path=True, accepted_statuses=(503,)
        )
        return HealthReport.model_validate(response.json())

    def db(self) -> HealthCheck:
        response = self._http.request(
            "GET", "/health/db", root_path=True, accepted_statuses=(503,)
        )
        return HealthCheck.model_validate(response.json())

    def redis(self) -> HealthCheck:
        response = self._http.request(
            "GET", "/health/redis", root_path=True, accepted_statuses=(503,)
        )
        return HealthCheck.model_validate(response.json())

    def minio(self) -> HealthCheck:
        response = self._http.request(
            "GET", "/health/minio", root_path=True, accepted_statuses=(503,)
        )
        return HealthCheck.model_validate(response.json())

    def celery(self) -> HealthCheck:
        response = self._http.request(
            "GET", "/health/celery", root_path=True, accepted_statuses=(503,)
        )
        return HealthCheck.model_validate(response.json())
