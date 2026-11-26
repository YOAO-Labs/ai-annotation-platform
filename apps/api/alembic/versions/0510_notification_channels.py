"""Add external notification channels, preferences v2, outbox, and narrow visibility checks."""

from importlib.resources import files
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0510"
down_revision = "bo0509"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"


def _execute(statement: str) -> None:
    stripped = statement.strip()
    if not stripped:
        return
    op.execute(
        sa.text(stripped.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    notifications_sql = files("yoao_notifications.sql").joinpath(
        "0510_notification_channels.sql"
    )
    for statement in notifications_sql.read_text(encoding="utf-8").split(_BREAK):
        _execute(statement)

    visibility_sql = _SQL / "0510_notification_target_visibility.sql"
    for statement in visibility_sql.read_text(encoding="utf-8").split(_BREAK):
        _execute(statement)


def downgrade() -> None:
    for signature in (
        "yoao_labeling_v1.notification_target_visible_to_actor(uuid,text,uuid,uuid,uuid)",
        "yoao_labeling_v1._notification_task_visible(uuid,text,uuid,uuid)",
        "yoao_diagnostics_v1.bug_report_visible_to_actor(uuid,uuid)",
        "yoao_notifications_v1.finish_channel_delivery(jsonb)",
        "yoao_notifications_v1.claim_channel_deliveries(jsonb)",
        "yoao_notifications_v1.channel_delivery_policy()",
        "yoao_notifications_v1.has_terminal_channel_delivery(uuid,text,uuid)",
        "yoao_notifications_v1.enqueue_channel_deliveries(jsonb)",
        "yoao_notifications_v1.delete_user_channel_binding(uuid,text)",
        "yoao_notifications_v1.mark_user_channel_binding_verified(uuid,text,timestamptz)",
        "yoao_notifications_v1.upsert_user_channel_binding(jsonb)",
        "yoao_notifications_v1.list_user_channel_bindings(uuid)",
        "yoao_notifications_v1.open_notification_channel_secret(text)",
        "yoao_notifications_v1.transition_notification_channel(jsonb)",
        "yoao_notifications_v1.register_notification_channel(jsonb)",
        "yoao_notifications_v1.get_notification_channel(text)",
        "yoao_notifications_v1.list_notification_channels(boolean)",
        "yoao_notifications_v1._channel_public_projection(yoao_notifications_v1.notification_channels)",
        "yoao_notifications_v1._preference_policy_v2()",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")

    source = next(
        statement.strip()
        for statement in (_SQL / "0311_notifications_preferences.sql")
        .read_text(encoding="utf-8")
        .split(_BREAK)
        if statement.lstrip().startswith(
            "CREATE FUNCTION aap_notifications_v1.preference_command("
        )
    )
    _execute(
        source.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1).replace(
            "aap_notifications_v1.", "yoao_notifications_v1."
        )
    )

    op.drop_table("notification_channel_deliveries", schema="yoao_notifications_v1")
    op.drop_table("user_notification_channel_bindings", schema="yoao_notifications_v1")
    op.drop_table("notification_channels", schema="yoao_notifications_v1")
