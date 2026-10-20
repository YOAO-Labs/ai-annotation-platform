from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from aap_identity.contract import IdentityUnavailable
from app.services.identity_contracts import IdentityGatewayUnavailable

_DSN = "postgresql://owner.example/identity"
_CASES = (
    (
        "account",
        "app.services.identity_account_commands",
        "IdentityAccountCommands",
        "AccountPostgresData",
        "IdentityAccountCommandData",
        "AccountManager",
        "identity_account_database_url",
        "identity_account_database_url_file",
        "account_policy",
        "execute",
    ),
    (
        "keys",
        "app.services.api_key_service",
        "IdentityKeyManagement",
        "KeyPostgresData",
        "IdentityKeySessionData",
        "KeyManager",
        "identity_key_database_url",
        "identity_key_database_url_file",
        "key_policy",
        "execute",
    ),
    (
        "sessions",
        "app.services.identity_session_commands",
        "IdentitySessionCommands",
        "SessionPostgresData",
        "IdentitySessionCommandData",
        "SessionManager",
        "identity_session_database_url",
        "identity_session_database_url_file",
        "session_policy",
        "execute",
    ),
    (
        "groups",
        "app.services.identity_group_commands",
        "IdentityGroupCommands",
        "GroupPostgresData",
        "IdentityGroupCommandData",
        "GroupManager",
        "identity_group_database_url",
        "identity_group_database_url_file",
        "group_policy",
        "execute",
    ),
    (
        "directory",
        "app.services.identity_directory_commands",
        "IdentityDirectoryCommands",
        "DirectoryPostgresData",
        "IdentityDirectoryCommandData",
        "DirectoryManager",
        "identity_directory_database_url",
        "identity_directory_database_url_file",
        "directory_policy",
        "execute",
    ),
    (
        "recovery",
        "app.services.identity_recovery_commands",
        "IdentityRecoveryCommands",
        "RecoveryPostgresData",
        "IdentityRecoveryCommandData",
        "RecoveryManager",
        "identity_recovery_database_url",
        "identity_recovery_database_url_file",
        "recovery_policy",
        "execute",
    ),
    (
        "invitations",
        "app.services.identity_invitation_commands",
        "IdentityInvitationCommands",
        "InvitationPostgresData",
        "IdentityInvitationCommandData",
        "InvitationManager",
        "identity_invitation_database_url",
        "identity_invitation_database_url_file",
        "invitation_policy",
        "execute",
    ),
    (
        "deactivation",
        "app.services.identity_deactivation_commands",
        "IdentityDeactivationCommands",
        "DeactivationPostgresData",
        "IdentityDeactivationCommandData",
        "DeactivationManager",
        "identity_deactivation_database_url",
        "identity_deactivation_database_url_file",
        "deactivation_policy",
        "execute",
    ),
    (
        "deletions",
        "app.services.identity_deletion_commands",
        "IdentityDeletionCommands",
        "DeletionPostgresData",
        "IdentityDeletionCommandData",
        "DeletionManager",
        "identity_deletion_database_url",
        "identity_deletion_database_url_file",
        "deletion_policy",
        "execute",
    ),
    (
        "offboarding",
        "app.services.identity_offboarding_commands",
        "IdentityOffboardingCommands",
        "OffboardingPostgresData",
        "IdentityOffboardingCommandData",
        "OffboardingManager",
        "identity_offboarding_database_url",
        "identity_offboarding_database_url_file",
        "offboarding_policy",
        "execute",
    ),
    (
        "deactivation_due",
        "app.services.identity_deactivation_commands",
        "IdentityDeactivationDueCommands",
        "DeactivationDuePostgresData",
        "IdentityDeactivationDueData",
        None,
        "identity_deactivation_operator_database_url",
        "identity_deactivation_operator_database_url_file",
        "due_policy",
        "process_due_deactivations",
    ),
)


def _settings(url_field, file_field):
    values = {
        "identity_account_database_url": "",
        "identity_account_database_url_file": "",
        "identity_key_database_url": "",
        "identity_key_database_url_file": "",
        "identity_session_database_url": "",
        "identity_session_database_url_file": "",
        "identity_group_database_url": "",
        "identity_group_database_url_file": "",
        "identity_directory_database_url": "",
        "identity_directory_database_url_file": "",
        "identity_recovery_database_url": "",
        "identity_recovery_database_url_file": "",
        "identity_invitation_database_url": "",
        "identity_invitation_database_url_file": "",
        "identity_deactivation_database_url": "",
        "identity_deactivation_database_url_file": "",
        "identity_deactivation_operator_database_url": "",
        "identity_deactivation_operator_database_url_file": "",
        "secret_key": "readiness-test-secret",
        "access_token_expire_minutes": 60,
        "email_verification_required": False,
        "allow_open_registration": False,
        "frontend_base_url": "https://frontend.example.test",
        "login_captcha_threshold": 5,
        "invitation_ttl_days": 7,
        "max_invitations_per_day": 30,
    }
    values[url_field] = _DSN
    values[file_field] = ""
    return SimpleNamespace(**values)


def _bind_adapter(monkeypatch, case, *, reject_readiness):
    (
        _name,
        module_name,
        service_name,
        postgres_name,
        caller_name,
        manager_name,
        url_field,
        file_field,
        policy_method,
        _execute_method,
    ) = case
    module = importlib.import_module(module_name)
    events = []

    class FakePostgresData:
        def __init__(self, dsn):
            assert dsn == _DSN
            events.append("data")

        async def readiness(self):
            events.append("policy")
            if reject_readiness:
                raise IdentityUnavailable()

        async def process_due_deactivations(self, _request):
            events.append("execute")
            assert events == ["data", "policy", "execute"]
            return "processed"

    setattr(FakePostgresData, policy_method, FakePostgresData.readiness)
    monkeypatch.setattr(module, postgres_name, FakePostgresData)
    monkeypatch.setattr(module, "settings", _settings(url_field, file_field))

    def forbidden_caller(*_args, **_kwargs):
        events.append("caller_fallback")
        raise AssertionError("Explicit owner readiness failure selected caller SQL")

    monkeypatch.setattr(module, caller_name, forbidden_caller)

    if manager_name is not None:

        class FakeManager:
            def __init__(self, *args, **_kwargs):
                self.data = next(
                    value for value in args if isinstance(value, FakePostgresData)
                )
                events.append("manager")

            async def execute(self, _request):
                assert events == ["data", "policy", "manager"]
                events.append("execute")
                return "processed"

        monkeypatch.setattr(module, manager_name, FakeManager)

    if module_name == "app.services.identity_recovery_commands":
        monkeypatch.setattr(module, "recovery_mail_key", lambda _secret: "mail-key")
    elif module_name == "app.services.identity_invitation_commands":
        monkeypatch.setattr(module, "recovery_mail_key", lambda _secret: "mail-key")

    service = getattr(module, service_name)(
        object(),
        SimpleNamespace(
            identity_url=None,
            timeout_seconds=5,
            signing_secret="readiness-test-secret",
        ),
    )
    request = SimpleNamespace(operation="create_invitation")
    return service, request, events


async def _execute(case, service, request):
    execute_method = case[-1]
    return await getattr(service, execute_method)(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[case[0] for case in _CASES])
async def test_direct_owner_dsn_checks_policy_before_business_command(
    monkeypatch, case
):
    service, request, events = _bind_adapter(monkeypatch, case, reject_readiness=False)

    assert await _execute(case, service, request) == "processed"
    expected = (
        ["data", "policy", "execute"]
        if case[5] is None
        else ["data", "policy", "manager", "execute"]
    )
    assert events == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=[case[0] for case in _CASES])
async def test_direct_owner_readiness_failure_neither_executes_nor_falls_back(
    monkeypatch, case
):
    service, request, events = _bind_adapter(monkeypatch, case, reject_readiness=True)

    with pytest.raises(IdentityGatewayUnavailable):
        await _execute(case, service, request)
    assert events == ["data", "policy"]
