"""Explicit account and session operations."""

from __future__ import annotations

from urllib.parse import quote
from ai_annotation._http import HttpTransport
from ai_annotation.auth_models import (
    AuthAcceptInvitationRequest,
    AuthAcceptInvitationResponse,
    AuthForgotPasswordRequest,
    AuthInvitationResolve,
    AuthLoginRequest,
    AuthMessage,
    AuthOpenRegisterRequest,
    AuthRegistrationStatus,
    AuthRegisterRequest,
    AuthRegisterResponse,
    AuthResetPasswordRequest,
    AuthSendVerificationEmailRequest,
    AuthToken,
    AuthVerifyEmailRequest,
    _auth_body,
)


class Auth:
    """Login, registration, invitation, verification, and session endpoints.

    Registration, password recovery, verification, invitation resolution, and
    login are public routes. Existing-user invitation acceptance and ``logout_all``
    use the server's authenticated-user dependency (JWT or API key); acceptance
    still requires a matching invitation email. ``refresh`` and ``logout`` require
    a JWT. Returned tokens are never saved or rotated in the ``Client``.

    POST operations are sent once. The shared transport retains its standard
    idempotent GET retry behavior for invitation resolution and registration status.
    """

    def __init__(self, http: HttpTransport):
        self._http = http

    def forgot_password(
        self, email: str, *, captcha_token: str | None = None
    ) -> AuthMessage:
        request = AuthForgotPasswordRequest(email=email, captcha_token=captcha_token)
        response = self._http.request(
            "POST", "/auth/forgot-password", json=_auth_body(request)
        )
        return AuthMessage.model_validate(response.json())

    def accept_invitation(self, token: str) -> AuthAcceptInvitationResponse:
        """Accept an invitation for the current account when its email matches."""
        request = AuthAcceptInvitationRequest(token=token)
        response = self._http.request(
            "POST", "/auth/invitations/accept", json=_auth_body(request)
        )
        return AuthAcceptInvitationResponse.model_validate(response.json())

    def login(
        self,
        email: str,
        password: str,
        *,
        captcha_token: str | None = None,
    ) -> AuthToken:
        request = AuthLoginRequest(
            email=email, password=password, captcha_token=captcha_token
        )
        response = self._http.request("POST", "/auth/login", json=_auth_body(request))
        return AuthToken.model_validate(response.json())

    def logout(self) -> None:
        """Revoke the configured JWT session; API-key credentials are unsupported."""
        self._http.request("POST", "/auth/logout")

    def logout_all(self) -> AuthToken:
        """Revoke all JWT sessions for the configured principal and return a new JWT."""
        response = self._http.request("POST", "/auth/logout-all")
        return AuthToken.model_validate(response.json())

    def refresh(self) -> AuthToken:
        """Exchange the configured JWT for a new token without changing Client state."""
        response = self._http.request("POST", "/auth/refresh")
        return AuthToken.model_validate(response.json())

    def register(self, token: str, name: str, password: str) -> AuthRegisterResponse:
        request = AuthRegisterRequest(token=token, name=name, password=password)
        response = self._http.request(
            "POST", "/auth/register", json=_auth_body(request)
        )
        return AuthRegisterResponse.model_validate(response.json())

    def register_open(
        self,
        email: str,
        name: str,
        password: str,
        *,
        captcha_token: str | None = None,
    ) -> AuthRegisterResponse:
        request = AuthOpenRegisterRequest(
            email=email,
            name=name,
            password=password,
            captcha_token=captcha_token,
        )
        response = self._http.request(
            "POST", "/auth/register-open", json=_auth_body(request)
        )
        return AuthRegisterResponse.model_validate(response.json())

    def reset_password(self, token: str, new_password: str) -> AuthMessage:
        request = AuthResetPasswordRequest(token=token, new_password=new_password)
        response = self._http.request(
            "POST", "/auth/reset-password", json=_auth_body(request)
        )
        return AuthMessage.model_validate(response.json())

    def send_verification_email(self, email: str) -> AuthMessage:
        request = AuthSendVerificationEmailRequest(email=email)
        response = self._http.request(
            "POST", "/auth/send-verification-email", json=_auth_body(request)
        )
        return AuthMessage.model_validate(response.json())

    def verify_email(self, token: str) -> AuthMessage:
        request = AuthVerifyEmailRequest(token=token)
        response = self._http.request(
            "POST", "/auth/verify-email", json=_auth_body(request)
        )
        return AuthMessage.model_validate(response.json())

    def resolve_invitation(self, token: str) -> AuthInvitationResolve:
        if not isinstance(token, str) or not token:
            raise ValueError("invitation token must be a non-empty string")
        token = quote(token, safe="")
        response = self._http.request("GET", f"/auth/invitations/{token}")
        return AuthInvitationResolve.model_validate(response.json())

    def registration_status(self) -> AuthRegistrationStatus:
        response = self._http.request("GET", "/auth/registration-status")
        return AuthRegistrationStatus.model_validate(response.json())
