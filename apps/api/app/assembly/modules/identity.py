"""Identity built-in module manifest."""

from __future__ import annotations

from app.assembly.contracts import (
    BeatScheduleSpec,
    BuiltinModuleManifest,
    ComposeOverlaySpec,
    CronSpec,
    MigrationSurface,
    RouterMount,
)

MANIFEST = BuiltinModuleManifest(
    module_id="identity",
    owner_packages=(
        "yoao-identity",
        "yoao-identity-admission",
        "yoao-mail-transport",
    ),
    routers=(
        RouterMount(module_path="app.api.v1.auth", prefix="/auth", tags=("auth",)),
        RouterMount(
            module_path="app.api.v1.invitations", prefix="/auth", tags=("auth",)
        ),
        RouterMount(
            module_path="app.api.v1.invitations_admin",
            prefix="/invitations",
            tags=("invitations",),
        ),
        RouterMount(module_path="app.api.v1.me", prefix="/auth/me", tags=("me",)),
        RouterMount(
            module_path="app.api.v1.api_keys",
            prefix="/me/api-keys",
            tags=("api-keys",),
        ),
        RouterMount(
            module_path="app.api.v1.avatars", prefix="/avatars", tags=("avatars",)
        ),
        RouterMount(module_path="app.api.v1.users", prefix="/users", tags=("users",)),
        RouterMount(
            module_path="app.api.v1.groups", prefix="/groups", tags=("groups",)
        ),
    ),
    celery_includes=(
        "app.workers.avatar_cleanup",
        "app.workers.deactivation",
        "app.workers.presence",
    ),
    beat_schedules=(
        BeatScheduleSpec(
            name="purge-retired-avatars",
            task="app.workers.avatar_cleanup.purge_retired_avatars",
            schedule=300.0,
        ),
        BeatScheduleSpec(
            name="process-deactivation-requests",
            task="app.workers.deactivation.process_deactivation_requests",
            schedule=CronSpec(hour=4, minute=0),
        ),
        BeatScheduleSpec(
            name="mark-inactive-offline",
            task="app.workers.presence.mark_inactive_offline",
            schedule=CronSpec(minute="*/2"),
        ),
    ),
    migrations=MigrationSurface(
        orm_models=(
            "user",
            "group",
            "organization",
            "api_key",
            "avatar_upload",
            "user_invitation",
            "password_reset_token",
            "email_verification_token",
        ),
        native_managed_tables=(
            ("public", "identity_revoked_jtis"),
            ("public", "identity_api_key_usage"),
            ("public", "identity_key_operation_receipts"),
            ("public", "identity_session_operation_receipts"),
            ("public", "identity_account_operation_receipts"),
            ("public", "identity_account_presence"),
            ("public", "identity_recovery_operation_receipts"),
            ("public", "identity_credential_mail_outbox"),
            ("public", "identity_group_operation_receipts"),
            ("public", "identity_deactivation_operation_receipts"),
            ("public", "identity_deactivation_due_receipts"),
            ("public", "identity_deactivation_requests"),
            ("public", "identity_invitation_operation_receipts"),
            ("public", "identity_directory_export_receipts"),
            ("public", "identity_admin_password_reset_receipts"),
            ("public", "identity_platform_role_operation_receipts"),
            ("public", "identity_suspension_operation_receipts"),
            ("public", "identity_deletion_operation_receipts"),
            ("public", "identity_offboarding_operation_receipts"),
        ),
    ),
    compose_overlays=(
        ComposeOverlaySpec(
            file="docker-compose.identity.yml",
            tier="optional_split",
            services=("identity-issuer", "identity-mail-v2"),
        ),
    ),
)
