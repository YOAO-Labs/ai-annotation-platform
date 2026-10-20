from __future__ import annotations

from pathlib import Path


def test_mail_v2_migration_keeps_v1_claim_renew_and_finish_recovery_only():
    repository = Path(__file__).resolve().parents[3]
    sql = (repository / "apps/api/alembic/sql/0242_invitation_mail.sql").read_text()
    frozen = (
        repository / "apps/api/alembic/sql/0239_identity_recovery.sql"
    ).read_text()

    def function_block(source: str, name: str, *, replacement: bool) -> str:
        create = "CREATE OR REPLACE FUNCTION" if replacement else "CREATE FUNCTION"
        start_marker = f"{create} aap_identity_recovery_v1.{name}(p_request jsonb)"
        revoke_marker = f"REVOKE ALL ON FUNCTION aap_identity_recovery_v1.{name}(jsonb) FROM PUBLIC;"
        start = source.index(start_marker)
        end = source.index(revoke_marker, start) + len(revoke_marker)
        return source[start:end]

    old_filter = """        WHERE ((outbox.status = 'pending' AND outbox.available_at <= v_now)
           OR (outbox.status = 'leased' AND outbox.lease_expires_at <= v_now))"""
    recovery_filter = """        WHERE outbox.purpose IN ('password_reset', 'email_verification')
          AND ((outbox.status = 'pending' AND outbox.available_at <= v_now)
           OR (outbox.status = 'leased' AND outbox.lease_expires_at <= v_now))"""
    stale_reply = """    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    SELECT account.email, account.is_active, account.email_verified_at"""
    v1_guard = """    IF NOT FOUND THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    IF v_purpose NOT IN ('password_reset', 'email_verification') THEN
        RETURN pg_catalog.jsonb_build_object(
            'schema', 'aap-identity-recovery-mail-native/v1', 'ok', true,
            'data', pg_catalog.jsonb_build_object('accepted', false)
        );
    END IF;
    SELECT account.email, account.is_active, account.email_verified_at"""

    original_claim = function_block(frozen, "claim_mail", replacement=False)
    expected_claim = original_claim.replace(
        "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
    ).replace(old_filter, recovery_filter, 1)
    actual_claim = function_block(sql, "claim_mail", replacement=True)
    assert actual_claim == expected_claim

    for name in ("renew_mail", "finish_mail"):
        expected = function_block(frozen, name, replacement=False).replace(
            "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
        )
        expected = expected.replace(stale_reply, v1_guard, 1)
        assert function_block(sql, name, replacement=True) == expected

    claim = sql.split(
        "CREATE OR REPLACE FUNCTION aap_identity_recovery_v1.claim_mail", 1
    )[1].split("REVOKE ALL ON FUNCTION aap_identity_recovery_v1.claim_mail", 1)[0]
    assert "outbox.purpose IN ('password_reset', 'email_verification')" in claim

    for name in ("renew_mail", "finish_mail"):
        function = sql.split(
            f"CREATE OR REPLACE FUNCTION aap_identity_recovery_v1.{name}", 1
        )[1].split(f"REVOKE ALL ON FUNCTION aap_identity_recovery_v1.{name}", 1)[0]
        assert "v_purpose NOT IN ('password_reset', 'email_verification')" in function

    assert "challenge_id DROP NOT NULL" in sql
    assert "ADD COLUMN invitation_id uuid" in sql
    assert "ADD COLUMN invitation_context jsonb" in sql
    assert "aap_identity_mail_v2.claim_mail(jsonb)" in sql
    assert "aap_identity_mail_v2.renew_mail(jsonb)" in sql
    assert "aap_identity_mail_v2.finish_mail(jsonb)" in sql
