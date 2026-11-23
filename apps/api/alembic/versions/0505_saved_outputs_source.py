"""Bind registered saved-output I/O to its durable owner and Source receipt."""

from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0505"
down_revision = "bo0504"
branch_labels = depends_on = None


def upgrade() -> None:
    resource = Path(__file__).resolve().parents[1] / "sql" / "0505_saved_outputs_source.sql"
    for statement in resource.read_text(encoding="utf-8").split("-- aap-statement-break"):
        op.execute(sa.text(statement.replace(":", r"\:")).execution_options(no_parameters=True))


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_avatar_source_write ON public.avatar_uploads")
    op.execute("DROP TRIGGER trg_bug_attachment_source_write ON public.bug_attachment_uploads")
    op.execute("DROP TRIGGER trg_audit_archive_source_write ON public.audit_archive_outputs")
    for name in (
        "_guard_saved_output_write()",
        "saved_output_write_status(uuid,text,text)",
        "finish_saved_output_write(uuid,text,text,jsonb)",
        "begin_saved_output_write(uuid,text,text,jsonb)",
        "saved_archive_subject(uuid,uuid,text)",
        "saved_avatar_subject(text,uuid,text)",
        "admit_saved_output_read(uuid,text,uuid,uuid,uuid,text)",
        "saved_bug_subject(uuid,uuid,text,uuid)",
        "saved_output_origin(jsonb,text,text)",
        "_saved_output_origin(uuid,text)",
        "_saved_output_gate(uuid,uuid,text)",
    ):
        op.execute(f"DROP FUNCTION yoao_assets_v1.{name}")
    op.execute("DROP FUNCTION yoao_audit_v1.saved_archive_output_facts(uuid,text,uuid)")
    op.execute("DROP TABLE yoao_assets_v1.saved_output_admissions")
    op.execute("DROP TABLE yoao_assets_v1.saved_output_writes")
