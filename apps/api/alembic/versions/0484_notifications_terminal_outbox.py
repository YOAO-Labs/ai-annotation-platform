"""Commit async-job terminal inbox rows with their Notifications delivery lease."""

from importlib.resources import files
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "bo0484"
down_revision = "bo0483"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_BREAK = "-- aap-statement-break"


def _execute(statement: str) -> None:
    op.execute(
        sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True)
    )


def upgrade() -> None:
    resource = files("yoao_notifications.sql").joinpath(
        "0484_notifications_terminal_outbox.sql"
    )
    for statement in resource.read_text(encoding="utf-8").split(_BREAK):
        _execute(statement)


def downgrade() -> None:
    op.execute(
        "LOCK TABLE public.notification_delivery_outbox IN ACCESS EXCLUSIVE MODE"
    )
    _execute("""
        DO $function$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.notification_delivery_outbox
                WHERE notification_type IN ('job.completed', 'job.failed', 'job.cancelled')
                  AND state = 'pending'
            ) THEN
                RAISE EXCEPTION 'Drain pending terminal notifications before downgrade';
            END IF;
        END
        $function$;
    """)
    for name, signature in (
        ("0321_notifications_terminal_append.sql", "append_job_terminal_notification"),
        ("0241_deactivation_notifications.sql", "claim_identity_deliveries"),
    ):
        declaration = f"CREATE FUNCTION aap_notifications_v1.{signature}("
        source = next(
            statement.strip()
            for statement in (_SQL / name).read_text(encoding="utf-8").split(_BREAK)
            if statement.lstrip().startswith(declaration)
        )
        _execute(
            source.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1).replace(
                "aap_notifications_v1.", "yoao_notifications_v1."
            )
        )
    op.execute("""
        DELETE FROM public.notification_delivery_outbox
        WHERE notification_type IN ('job.completed', 'job.failed', 'job.cancelled')
          AND state = 'done'
    """)
    op.execute("""
        ALTER TABLE public.notification_delivery_outbox
            DROP CONSTRAINT notification_delivery_outbox_notification_type_check,
            ADD CONSTRAINT notification_delivery_outbox_notification_type_check CHECK (
                notification_type IN ('user.deactivation_requested', 'user.deactivation_completed')
            )
    """)
    op.execute("""
        COMMENT ON TABLE public.notification_delivery_outbox IS
            'Notifications-owned at-least-once Redis handoffs for committed self-deactivation notices.'
    """)
