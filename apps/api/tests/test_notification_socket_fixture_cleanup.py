from uuid import UUID

from sqlalchemy.dialects import postgresql

from tests.test_notifications_socket_live import (
    _socket_fixture_user_cleanup_statement,
)


def test_socket_fixture_cleanup_preserves_audited_users_until_database_teardown():
    fixture_user_ids = [
        UUID("c3ce8749-59c8-4fb4-9e16-8a31a7f20401"),
        UUID("38a2c6bb-cb8f-4951-9e61-16d4c3bc98d3"),
    ]

    compiled = _socket_fixture_user_cleanup_statement(fixture_user_ids).compile(
        dialect=postgresql.dialect()
    )
    sql = " ".join(str(compiled).split())

    assert "DELETE FROM users WHERE users.id IN (__[POSTCOMPILE_id_1])" in sql
    assert (
        "AND NOT (EXISTS (SELECT 1 FROM aap_audit_v1.audit_events AS audit_events "
        "WHERE audit_events.actor_id = users.id))"
    ) in sql
    assert "public.audit_logs" not in sql
    assert compiled.params == {"id_1": fixture_user_ids}
