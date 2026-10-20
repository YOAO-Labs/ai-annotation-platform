"""Generate the distributable Identity wire schema from the closed models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from aap_identity.account_contract import (  # noqa: E402
    AccountFailureReply,
    AccountReply,
    AccountRequest,
    NativeAccountFailureReply,
    NativeAccountPolicyReply,
    NativeAccountReply,
    NativeApplyCommand,
    NativePrepareCommand,
    NativePrepareReply,
)
from aap_identity.contract import (  # noqa: E402
    AuthenticateRequest,
    CandidatesPage,
    FailureReply,
    PrincipalReply,
)
from aap_identity.deactivation_contract import (  # noqa: E402
    DeactivationDueFailureReply,
    DeactivationDuePolicyReply,
    DeactivationDueReply,
    DeactivationDueRequest,
    DeactivationFailureReply,
    DeactivationPolicyReply,
    DeactivationReply,
    DeactivationRequest,
    NativeDeactivationCommand,
    NativeDeactivationDueCommand,
    NativeDeactivationDueFailureReply,
    NativeDeactivationDueSuccess,
    NativeDeactivationFailureReply,
    NativeDeactivationSuccess,
)
from aap_identity.group_contract import (  # noqa: E402
    GroupFailureReply,
    GroupPolicyReply,
    GroupReply,
    GroupRequest,
    NativeGroupCommand,
    NativeGroupFailureReply,
    NativeGroupSuccess,
)
from aap_identity.notification_delivery_contract import (  # noqa: E402
    NotificationClaimReply,
    NotificationClaimRequest,
    NotificationDeliveryPolicyReply,
    NotificationFinishReply,
    NotificationFinishRequest,
    NotificationLease,
)
from aap_identity.key_contract import (  # noqa: E402
    KeyFailureReply,
    KeyReply,
    KeyRequest,
    NativeKeyCommand,
    NativeKeyFailureReply,
    NativeKeyReply,
)
from aap_identity.mail_contract import (  # noqa: E402
    MailAcceptedReply,
    MailClaimReply,
    MailClaimRequest,
    MailDeliveryPolicy,
    MailFinishRequest,
    MailPolicyReply,
    MailRenewRequest,
)
from aap_identity.preference_models import (  # noqa: E402
    UserPreferences,
    UserPreferencesRead,
)
from aap_identity.presence_contract import (  # noqa: E402
    PresenceReply,
    PresenceRequest,
)
from aap_identity.recovery_contract import (  # noqa: E402
    NativeRecoveryApplyCommand,
    NativeRecoveryApplyReply,
    NativeRecoveryFailureReply,
    NativeRecoveryPolicyReply,
    NativeRecoveryPrepareCommand,
    NativeRecoveryPrepareReply,
    RecoveryFailureReply,
    RecoveryReply,
    RecoveryRequest,
)
from aap_identity.session_contract import (  # noqa: E402
    LoginCandidateReply,
    NativeSessionCommand,
    NativeSessionFailureReply,
    NativeSessionReply,
    SessionFailureReply,
    SessionPolicy,
    SessionPolicyReply,
    SessionReply,
    SessionRequest,
)
from pydantic import TypeAdapter  # noqa: E402


def principal_shape(schema):
    schema["$defs"]["Principal"]["allOf"] = [
        {
            "oneOf": [
                {
                    "properties": {
                        "credential_type": {"const": "jwt"},
                        "generation": {
                            "type": "integer",
                            "minimum": 0,
                            "exclusiveMaximum": 2**63,
                        },
                        "scopes": {"type": "null"},
                        "expires_at": {"type": "string", "format": "date-time"},
                    }
                },
                {
                    "properties": {
                        "credential_type": {"const": "api_key"},
                        "generation": {"type": "null"},
                        "scopes": {"type": "array", "items": {"type": "string"}},
                    }
                },
            ]
        }
    ]
    return schema


def patch_shape(schema):
    patch = schema.get("$defs", {}).get("KeyPatch")
    if patch is not None:
        patch["properties"]["name"] = {
            "maxLength": 60,
            "minLength": 1,
            "title": "Name",
            "type": "string",
        }
        patch["properties"]["scopes"] = {
            "items": {"type": "string"},
            "title": "Scopes",
            "type": "array",
        }
    return schema


def render_schema(adapter, schema_id):
    schema = adapter.json_schema()
    if "Principal" in schema.get("$defs", {}):
        principal_shape(schema)
    if "KeyPatch" in schema.get("$defs", {}):
        patch_shape(schema)
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = schema_id
    return schema


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schemas = {
        "src/aap_identity/protocol.schema.json": render_schema(
            TypeAdapter(
                AuthenticateRequest | PrincipalReply | FailureReply | CandidatesPage
            ),
            "aap-identity/v1",
        ),
        "src/aap_identity/key_protocol.schema.json": render_schema(
            TypeAdapter(KeyRequest | KeyReply | KeyFailureReply),
            "aap-identity-keys/v1",
        ),
        "src/aap_identity/native_key_protocol.schema.json": render_schema(
            TypeAdapter(NativeKeyCommand | NativeKeyReply | NativeKeyFailureReply),
            "aap-identity-keys-native/v1",
        ),
        "src/aap_identity/session_protocol.schema.json": render_schema(
            TypeAdapter(SessionRequest | SessionReply | SessionFailureReply),
            "aap-identity-sessions/v1",
        ),
        "src/aap_identity/native_session_protocol.schema.json": render_schema(
            TypeAdapter(
                NativeSessionCommand
                | NativeSessionReply
                | NativeSessionFailureReply
                | SessionPolicy
                | SessionPolicyReply
                | LoginCandidateReply
            ),
            "aap-identity-sessions-native/v1",
        ),
        "src/aap_identity/account_protocol.schema.json": render_schema(
            TypeAdapter(AccountRequest | AccountReply | AccountFailureReply),
            "aap-identity-accounts/v1",
        ),
        "src/aap_identity/native_account_protocol.schema.json": render_schema(
            TypeAdapter(
                NativePrepareCommand
                | NativeApplyCommand
                | NativePrepareReply
                | NativeAccountReply
                | NativeAccountFailureReply
                | NativeAccountPolicyReply
            ),
            "aap-identity-accounts-native/v1",
        ),
        "src/aap_identity/presence_protocol.schema.json": render_schema(
            TypeAdapter(PresenceRequest | PresenceReply),
            "aap-identity-presence/v1",
        ),
        "src/aap_identity/recovery_protocol.schema.json": render_schema(
            TypeAdapter(RecoveryRequest | RecoveryReply | RecoveryFailureReply),
            "aap-identity-recovery/v1",
        ),
        "src/aap_identity/native_recovery_protocol.schema.json": render_schema(
            TypeAdapter(
                NativeRecoveryPrepareCommand
                | NativeRecoveryApplyCommand
                | NativeRecoveryPrepareReply
                | NativeRecoveryApplyReply
                | NativeRecoveryFailureReply
                | NativeRecoveryPolicyReply
            ),
            "aap-identity-recovery-native/v1",
        ),
        "src/aap_identity/group_protocol.schema.json": render_schema(
            TypeAdapter(GroupRequest | GroupReply | GroupFailureReply),
            "aap-identity-groups/v1",
        ),
        "src/aap_identity/native_group_protocol.schema.json": render_schema(
            TypeAdapter(
                NativeGroupCommand
                | NativeGroupSuccess
                | NativeGroupFailureReply
                | GroupPolicyReply
            ),
            "aap-identity-groups-native/v1",
        ),
        "src/aap_identity/deactivation_protocol.schema.json": render_schema(
            TypeAdapter(
                DeactivationRequest | DeactivationReply | DeactivationFailureReply
            ),
            "aap-identity-deactivation/v1",
        ),
        "src/aap_identity/native_deactivation_protocol.schema.json": render_schema(
            TypeAdapter(
                NativeDeactivationCommand
                | NativeDeactivationSuccess
                | NativeDeactivationFailureReply
                | DeactivationPolicyReply
            ),
            "aap-identity-deactivation-native/v1",
        ),
        "src/aap_identity/deactivation_due_protocol.schema.json": render_schema(
            TypeAdapter(
                DeactivationDueRequest
                | DeactivationDueReply
                | DeactivationDueFailureReply
            ),
            "aap-identity-deactivation-due/v1",
        ),
        "src/aap_identity/native_deactivation_due_protocol.schema.json": render_schema(
            TypeAdapter(
                NativeDeactivationDueCommand
                | NativeDeactivationDueSuccess
                | NativeDeactivationDueFailureReply
                | DeactivationDuePolicyReply
            ),
            "aap-identity-deactivation-due-native/v1",
        ),
        "src/aap_identity/notification_delivery_protocol.schema.json": render_schema(
            TypeAdapter(
                NotificationClaimRequest
                | NotificationClaimReply
                | NotificationFinishRequest
                | NotificationFinishReply
                | NotificationDeliveryPolicyReply
                | NotificationLease
            ),
            "aap-notifications-delivery/v1",
        ),
        "src/aap_identity/native_mail_protocol.schema.json": render_schema(
            TypeAdapter(
                MailClaimRequest
                | MailRenewRequest
                | MailFinishRequest
                | MailClaimReply
                | MailAcceptedReply
                | MailPolicyReply
            ),
            "aap-identity-recovery-mail-native/v1",
        ),
        "src/aap_identity/mail_settings_protocol.schema.json": render_schema(
            TypeAdapter(MailDeliveryPolicy), "aap-identity-mail-settings/v1"
        ),
        "src/aap_identity/preferences_write.schema.json": render_schema(
            TypeAdapter(UserPreferences), "aap-identity-preferences-write/v1"
        ),
        "src/aap_identity/preferences_read.schema.json": render_schema(
            TypeAdapter(UserPreferencesRead), "aap-identity-preferences-read/v1"
        ),
    }
    for relative_path, schema in schemas.items():
        target = ROOT / relative_path
        if args.check:
            if not target.exists() or json.loads(target.read_text()) != schema:
                raise SystemExit(
                    f"Identity schema differs from its frozen models: {relative_path}"
                )
        else:
            target.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
