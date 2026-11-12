"""Retry a settled failed publication without releasing its fixed identity."""

import re
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "bo0458"
down_revision = "bo0457"
branch_labels = depends_on = None

_SQL = Path(__file__).resolve().parents[1] / "sql"
_FROZEN = re.compile(
    r"CREATE OR REPLACE FUNCTION\s+"
    r"aap_assets_v1\.begin_operator_auxiliary\s*\("
    r".*?AS \$function\$.*?\$function\$;",
    re.DOTALL,
)
_DUPLICATE_RESERVED_KEYS = """
DO $$
DECLARE
    v_duplicates bigint;
BEGIN
    SELECT count(*) INTO v_duplicates
    FROM (
        SELECT 1
        FROM public.managed_original_creations
        GROUP BY storage_identity, bucket, object_key
        HAVING count(*) > 1
        LIMIT 1
    ) AS duplicate;
    IF v_duplicates = 0 THEN
        SELECT count(*) INTO v_duplicates
        FROM (
            SELECT 1
            FROM public.managed_original_creations
            WHERE staging_key IS NOT NULL
            GROUP BY staging_key
            HAVING count(*) > 1
            LIMIT 1
        ) AS duplicate;
    END IF;
    IF v_duplicates > 0 THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'reserved original keys have retry history',
            DETAIL = '{"code":"reserved_key_retry_history"}';
    END IF;
END $$;
"""


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
        (_SQL / "0458_upload_retry_after_failed_publication.sql").read_text(
            encoding="utf-8"
        )
    )


def downgrade() -> None:
    # The previous reserved-key constraint cannot represent a key that holds an
    # immutable failed attempt plus its retry. Refuse before changing anything;
    # the migration transaction keeps every function and index in place.
    _execute(_DUPLICATE_RESERVED_KEYS)
    _execute(
        """
DROP INDEX public.uq_managed_creation_reserved_key_active;
-- aap-statement-break
DROP INDEX public.uq_managed_creation_staging_key_active;
-- aap-statement-break
ALTER TABLE public.managed_original_creations
    ADD CONSTRAINT uq_managed_creation_reserved_key
    UNIQUE (storage_identity, bucket, object_key);
-- aap-statement-break
ALTER TABLE public.managed_original_creations
    ADD CONSTRAINT managed_original_creations_staging_key_key
    UNIQUE (staging_key);
"""
    )
    # Restore the exact pre-migration function body from the frozen artifact.
    matches = _FROZEN.findall(
        (_SQL / "0379_operator_auxiliary.sql").read_text(encoding="utf-8")
    )
    if len(matches) != 1:
        raise RuntimeError("frozen managed creation admission body is not unique")
    _execute(matches[0])
