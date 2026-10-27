"""Install the durable, versioned accepted image-input boundary.

Revision ID: bo0221
Revises: bo0220
"""

from __future__ import annotations

from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "bo0221"
down_revision = "bo0220"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_STATEMENT_SEPARATOR = "-- aap-statement-break"
_UPGRADE_ARTIFACTS = (
    "0221_accepted_inputs_state.sql",
    "0221_accepted_inputs_authorities.sql",
    "0221_accepted_inputs_control.sql",
    "0221_accepted_inputs_execution.sql",
    "0221_accepted_inputs_retention.sql",
    "0221_accepted_inputs_garbage.sql",
)
_SCHEMA_ARTIFACTS = ("0221_accepted_inputs.schema.json",)


def _statements(filename: str) -> list[str]:
    artifact = _SQL_DIR / filename
    return [
        statement.strip()
        for statement in artifact.read_text(encoding="utf-8").split(
            _STATEMENT_SEPARATOR
        )
        if statement.strip()
    ]


def _run_artifact(filename: str) -> None:
    for statement in _statements(filename):
        op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        _run_artifact(filename)


def _count(bind, query: str) -> int:
    return int(bind.execute(sa.text(query)).scalar_one())


def _function_signature(statement: str) -> str | None:
    match = re.match(
        r"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;\s*$",
        statement,
    )
    return " ".join(match.group(1).split()) if match else None


def _function_name(signature: str) -> str:
    return signature.split("(", 1)[0].strip().lower()


def _new_function_signatures() -> list[str]:
    signatures = []
    for filename in reversed(_UPGRADE_ARTIFACTS):
        for statement in reversed(_statements(filename)):
            signature = _function_signature(statement)
            if signature:
                signatures.append(signature)
    return signatures


def _trigger_drop_statements() -> list[str]:
    triggers: list[str] = []
    pattern = re.compile(
        r"(?is)^CREATE\s+(?:CONSTRAINT\s+)?TRIGGER\s+([a-z_][a-z0-9_]*)\s+"
        r".*?\s+ON\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\b"
    )
    for filename in _UPGRADE_ARTIFACTS:
        for statement in _statements(filename):
            match = pattern.match(statement)
            if match:
                trigger_name, relation = match.groups()
                triggers.append(f"DROP TRIGGER IF EXISTS {trigger_name} ON {relation}")
    return list(reversed(triggers))


def downgrade() -> None:
    bind = op.get_bind()
    retained = {
        "inputs": _count(
            bind, "SELECT count(*) FROM public.asset_accepted_image_inputs"
        ),
        "attempts": _count(
            bind, "SELECT count(*) FROM public.asset_accepted_image_input_attempts"
        ),
        "garbage": _count(
            bind, "SELECT count(*) FROM public.asset_accepted_input_garbage"
        ),
        "annotation_pins": _count(
            bind, "SELECT count(*) FROM public.annotation_input_sources"
        ),
    }
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            "Cannot downgrade retained accepted image input receipts or cleanup "
            f"responsibility: {detail}."
        )

    for statement in _trigger_drop_statements():
        op.execute(statement)

    # This SQL helper depends on the table's composite row type and must go
    # before the input table; the other helpers are dropped after constraints.
    op.execute(
        "DROP FUNCTION IF EXISTS aap_assets_v1._accepted_image_input_record("
        "public.asset_accepted_image_inputs) RESTRICT"
    )
    op.drop_table("annotation_input_sources", schema="public")
    op.drop_table("asset_accepted_input_garbage", schema="public")
    op.drop_table("asset_accepted_image_input_attempts", schema="public")
    op.drop_table("asset_accepted_image_inputs", schema="public")

    composite_function = "aap_assets_v1._accepted_image_input_record"
    for signature in _new_function_signatures():
        if _function_name(signature) == composite_function:
            continue
        op.execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
