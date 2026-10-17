"""Super-administrator system overrides with explicit version preconditions."""

from ai_annotation._http import HttpTransport
from ai_annotation.system_setting_models import (
    SmtpTestResult,
    SystemSettingsOut,
    SystemSettingsReset,
    SystemSettingsUpdate,
)


class SystemSettings:
    def __init__(self, http: HttpTransport):
        self._http = http

    def get(self) -> SystemSettingsOut:
        response = self._http.request("GET", "/settings/system")
        return SystemSettingsOut.model_validate(response.json())

    def update(self, body: SystemSettingsUpdate) -> SystemSettingsOut:
        response = self._http.request(
            "PATCH",
            "/settings/system",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return SystemSettingsOut.model_validate(response.json())

    def reset(self, body: SystemSettingsReset) -> SystemSettingsOut:
        response = self._http.request(
            "POST",
            "/settings/system/reset",
            json=body.model_dump(mode="json", exclude_unset=True),
        )
        return SystemSettingsOut.model_validate(response.json())

    def test_smtp(self) -> SmtpTestResult:
        """Send one real test email to the authenticated administrator."""
        response = self._http.request("POST", "/settings/system/test-smtp")
        return SmtpTestResult.model_validate(response.json())
