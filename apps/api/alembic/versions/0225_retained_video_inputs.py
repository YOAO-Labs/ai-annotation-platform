"""Install retained video manifests, exact frame inputs and evidence retention.

Revision ID: bo0225
Revises: bo0224
"""

from __future__ import annotations

from pathlib import Path
import re

from alembic import op
import sqlalchemy as sa

revision = "bo0225"
down_revision = "bo0224"
branch_labels = depends_on = None

_SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
_UPGRADE_ARTIFACTS = (
    "0225_video_inputs_state.sql",
    "0225_video_inputs_control.sql",
    "0225_video_inputs_garbage.sql",
    "0225_video_inputs_execution.sql",
    "0225_video_inputs_retention.sql",
)
_SCHEMA_ARTIFACTS = (
    "0225_video_inputs.schema.json",
    "0225_video_input_evidence.schema.json",
)
_TABLES = (
    "annotation_video_input_sources",
    "asset_accepted_video_frame_attempts",
    "asset_accepted_video_frames",
    "asset_accepted_video_times",
    "asset_accepted_video_input_attempts",
    "asset_accepted_video_inputs",
    "asset_accepted_video_members",
    "asset_accepted_video_manifests",
)
_REPLACED = {
    "aap_assets_v1.claim_accepted_input_garbage",
    "aap_assets_v1.complete_accepted_input_garbage",
}


def _statements(filename: str) -> list[str]:
    return [
        statement.strip()
        for statement in (_SQL_DIR / filename)
        .read_text(encoding="utf-8")
        .split("-- aap-statement-break")
        if statement.strip()
    ]


def _signatures() -> list[str]:
    result = []
    for filename in _UPGRADE_ARTIFACTS:
        for statement in _statements(filename):
            match = re.match(
                r"(?is)^REVOKE\s+ALL\s+ON\s+FUNCTION\s+(.+?)\s+FROM\s+PUBLIC\s*;\s*$",
                statement,
            )
            if match and match.group(1).split("(", 1)[0].strip() not in _REPLACED:
                result.append(" ".join(match.group(1).split()))
    return result


def _execute(statement: str) -> None:
    op.execute(sa.text(statement.replace(":", r"\:")))


def upgrade() -> None:
    for filename in _UPGRADE_ARTIFACTS:
        for statement in _statements(filename):
            _execute(statement)


def downgrade() -> None:
    bind = op.get_bind()
    retained = {
        table: int(
            bind.execute(sa.text(f"SELECT count(*) FROM public.{table}")).scalar_one()
        )
        for table in _TABLES
    }
    retained["video_garbage"] = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM public.asset_accepted_input_garbage "
                "WHERE aap_assets_v1._video_input_prefix_valid(prefix)"
            )
        ).scalar_one()
    )
    nonempty = {name: count for name, count in retained.items() if count}
    if nonempty:
        detail = ", ".join(f"{name}={count}" for name, count in nonempty.items())
        raise RuntimeError(
            "Cannot downgrade retained video receipts, evidence or cleanup responsibility: "
            + detail
        )

    pattern = re.compile(
        r"(?im)^CREATE\s+(?:CONSTRAINT\s+)?TRIGGER\s+([a-z_][a-z0-9_]*)\s+"
        r"[\s\S]*?\s+ON\s+([a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*)\b"
    )
    triggers = []
    for filename in _UPGRADE_ARTIFACTS:
        for statement in _statements(filename):
            match = pattern.search(statement)
            if match:
                name, table = match.groups()
                triggers.append(f"DROP TRIGGER IF EXISTS {name} ON {table}")
    for statement in reversed(triggers):
        _execute(statement)

    # Restore both image-only queue operations without replacing their ACLs.
    for statement in _statements("0221_accepted_inputs_garbage.sql"):
        if re.match(
            r"(?is)^.*?CREATE FUNCTION aap_assets_v1\.(claim|complete)_accepted_input_garbage\(",
            statement,
        ):
            _execute(
                statement.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
            )
    _execute(
        "ALTER TABLE public.asset_accepted_input_garbage DROP CONSTRAINT ck_asset_accepted_input_garbage_prefix"
    )
    _execute(
        "ALTER TABLE public.asset_accepted_input_garbage ADD CONSTRAINT "
        "ck_asset_accepted_input_garbage_prefix CHECK (aap_assets_v1._accepted_image_input_prefix_valid(prefix))"
    )

    signatures = list(reversed(_signatures()))
    composite = {
        signature
        for signature in signatures
        if any(re.search(r"\bpublic\." + table + r"\b", signature) for table in _TABLES)
    }
    for signature in signatures:
        if signature in composite:
            _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
    for table in _TABLES:
        op.drop_table(table, schema="public")
    for signature in signatures:
        if signature not in composite:
            _execute(f"DROP FUNCTION IF EXISTS {signature} RESTRICT")
