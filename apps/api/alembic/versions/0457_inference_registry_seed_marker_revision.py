"""Validate the seed marker namespace instead of one frozen seed revision."""

import re
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0457"
down_revision = "bo0456"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_FROZEN = re.compile(
    r"CREATE OR REPLACE FUNCTION\s+"
    r"aap_inference_registry_ingress_v1\.tag_seed_registry\s*\("
    r".*?AS \$function\$.*?\$function\$;",
    re.DOTALL,
)


def _execute(source: str) -> None:
    for statement in source.split("-- aap-statement-break"):
        if statement.strip():
            op.execute(
                sa.text(statement.strip().replace(":", r"\:")).execution_options(
                    no_parameters=True
                )
            )


def upgrade() -> None:
    _execute(
        (_SQL / "0457_inference_registry_seed_marker_revision.sql").read_text(
            encoding="utf-8"
        )
    )


def downgrade() -> None:
    # Migration 0388 renamed the original implementation and kept it as the
    # plugin wrapper's implementation; the forward migration changed only its
    # marker revision check, so the exact original body restores that contract.
    matches = _FROZEN.findall(
        (_SQL / "0278_inference_registry_ingress.sql").read_text(encoding="utf-8")
    )
    if len(matches) != 1:
        raise RuntimeError("frozen Registry seed marker body is not unique")
    _execute(
        matches[0].replace(
            "aap_inference_registry_ingress_v1.tag_seed_registry(",
            "aap_inference_registry_ingress_v1.tag_seed_registry_plugin_legacy(",
            1,
        )
    )
