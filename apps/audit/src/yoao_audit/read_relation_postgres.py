"""SQLAlchemy metadata for the fixed read-only Audit event relation.

Import this optional PostgreSQL adapter explicitly. The portable contract and
package root do not require SQLAlchemy.
"""

from sqlalchemy import BigInteger, Date, DateTime, SmallInteger, String, column, table
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID

from .read_relation_contract import AUDIT_EVENTS_RELATION
from .daily_projection_contract import AUDIT_DAILY_FACTS_RELATION

audit_events = table(
    "audit_events",
    column("id", BigInteger),
    column("actor_id", UUID(as_uuid=True)),
    column("actor_email", String(255)),
    column("actor_role", String(32)),
    column("action", String(64)),
    column("target_type", String(32)),
    column("target_id", String(64)),
    column("method", String(8)),
    column("path", String(256)),
    column("status_code", SmallInteger),
    column("ip", INET),
    column("detail_json", JSONB),
    column("request_id", String(36)),
    column("created_at", DateTime(timezone=True)),
    schema="aap_audit_v1",
).alias(AUDIT_EVENTS_RELATION.rsplit(".", 1)[-1])

audit_daily_facts = table(
    "audit_daily_facts",
    column("day", Date()),
    column("action", String(64)),
    column("target_type", String()),
    column("actor_role", String()),
    column("status_family", SmallInteger),
    column("event_count", BigInteger),
    schema=AUDIT_DAILY_FACTS_RELATION.rsplit(".", 1)[0],
).alias(AUDIT_DAILY_FACTS_RELATION.rsplit(".", 1)[-1])
