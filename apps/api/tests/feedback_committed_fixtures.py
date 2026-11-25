"""Committed owner sessions for independent Identity proof reads."""

from types import SimpleNamespace
import uuid

import httpx
import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token, hash_password
from app.db.models.api_key import ApiKey
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.bug_report import BugComment, BugReport
from app.db.models.user import User
from app.deps import get_db
from app.services import identity_request_authority
from app.services.identity_gateway import IdentityGateway, get_identity_gateway
from tests.test_reference_original_cache import referenced_cache as referenced_cache


@pytest.fixture
async def committed_feedback_world(referenced_cache, app_module, monkeypatch):
    owner = referenced_cache
    async with owner.factory() as db:
        actors = [user.id for user in (owner.actor, owner.stranger, owner.manager)]
        users = [await db.get(User, actor_id) for actor_id in actors]

        async def database():
            yield db

        monkeypatch.setitem(app_module.dependency_overrides, get_db, database)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_module), base_url="http://test"
        ) as client:

            def actor(user):
                return user, create_access_token(str(user.id), user.role)

            try:
                yield SimpleNamespace(
                    db=db,
                    annotator=actor(users[0]),
                    reviewer=actor(users[1]),
                    super_admin=actor(users[2]),
                    client=client,
                )
            finally:
                await db.rollback()
                reports = select(BugReport.id).where(BugReport.reporter_id.in_(actors))
                await db.execute(
                    delete(BugComment).where(BugComment.bug_report_id.in_(reports))
                )
                await db.execute(
                    delete(BugAttachmentUpload).where(
                        BugAttachmentUpload.owner_id.in_(actors)
                    )
                )
                await db.execute(
                    delete(BugReport).where(BugReport.reporter_id.in_(actors))
                )
                await db.commit()


@pytest.fixture
async def rollback_audit_world(referenced_cache, test_engine, app_module, monkeypatch):
    """Keep Identity committed while archive owners stay in one rollback-only transaction."""
    owner = referenced_cache
    gateway = IdentityGateway(
        auth_database_url=test_engine.url.render_as_string(hide_password=False),
        timeout_seconds=10,
    )
    monkeypatch.setitem(
        app_module.dependency_overrides, get_identity_gateway, lambda: gateway
    )
    monkeypatch.setattr(
        identity_request_authority, "get_identity_gateway", lambda: gateway
    )

    async def make_key(*, expires_at=None):
        token = "ak_" + uuid.uuid4().hex
        async with owner.factory() as identity:
            key = ApiKey(
                user_id=owner.manager.id,
                name="Archive read",
                key_prefix=token[:12],
                key_hash=hash_password(token),
                scopes=["datasets:read"],
                expires_at=expires_at,
            )
            identity.add(key)
            await identity.commit()
            return {"id": str(key.id), "plaintext": token}

    archive_ids = set()
    async with test_engine.connect() as connection:
        transaction = await connection.begin()
        db = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:

            @event.listens_for(db.sync_session, "after_flush")
            def remember(_session, _context):
                archive_ids.update(
                    row.id
                    for row in _session.new
                    if isinstance(row, AuditArchiveOutput)
                )

            admin = await db.get(User, owner.manager.id)

            async def database():
                yield db

            monkeypatch.setitem(app_module.dependency_overrides, get_db, database)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app_module), base_url="http://test"
            ) as client:
                yield SimpleNamespace(
                    db=db,
                    client=client,
                    super_admin=(admin, create_access_token(str(admin.id), admin.role)),
                    make_key=make_key,
                )
        finally:
            try:
                await db.close()
            finally:
                await transaction.rollback()
    if archive_ids:
        async with test_engine.connect() as observer:
            remaining = await observer.scalar(
                select(AuditArchiveOutput.id).where(
                    AuditArchiveOutput.id.in_(archive_ids)
                )
            )
            assert remaining is None, "rollback leaked an audit archive owner"
