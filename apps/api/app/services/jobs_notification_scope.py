"""SQL binding for Jobs' fixed public notification scope fact relation."""

from sqlalchemy import String, column, table
from sqlalchemy.dialects.postgresql import UUID

from aap_jobs.notification_scope_contract import NOTIFICATION_SCOPE_RELATION

_SCHEMA, _RELATION = NOTIFICATION_SCOPE_RELATION.split(".", 1)


def notification_scope_facts():
    return table(
        _RELATION,
        column("id", UUID(as_uuid=True)),
        column("kind", String(40)),
        column("scope_project_id", UUID(as_uuid=True)),
        schema=_SCHEMA,
    ).alias("jobs_notification_scope_facts")
