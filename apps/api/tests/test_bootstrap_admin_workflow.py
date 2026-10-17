import uuid

from sqlalchemy import func, select

from app.application.bootstrap_admin import bootstrap_admin
from app.db.enums import UserRole
from app.db.models.audit_log import AuditLog
from app.db.models.user import User


async def test_bootstrap_admin_is_idempotent_and_audits_only_creation(db_session):
    email = f"bootstrap-{uuid.uuid4()}@example.test"

    assert await bootstrap_admin(
        db_session, email=email, name="Initial Admin", password="initial-secret"
    )
    admin = await db_session.scalar(select(User).where(User.email == email))
    original_password_hash = admin.password_hash
    audit = await db_session.scalar(
        select(AuditLog).where(
            AuditLog.action == "system.bootstrap_admin",
            AuditLog.actor_id == admin.id,
        )
    )

    assert admin.role == UserRole.SUPER_ADMIN.value
    assert admin.is_active is True
    assert audit.target_type == "user"
    assert audit.target_id == str(admin.id)

    assert not await bootstrap_admin(
        db_session, email=email, name="Changed Name", password="replacement-secret"
    )
    await db_session.refresh(admin)
    assert admin.name == "Initial Admin"
    assert admin.password_hash == original_password_hash
    assert (
        await db_session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.action == "system.bootstrap_admin",
                AuditLog.actor_id == admin.id,
            )
        )
        == 1
    )
