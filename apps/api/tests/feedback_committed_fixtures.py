"""Committed owner sessions for independent Identity proof reads."""

from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import delete, event

from app.core.security import create_access_token
from app.db.models.audit_archive_output import AuditArchiveOutput
from app.db.models.bug_attachment_upload import BugAttachmentUpload
from app.db.models.bug_report import BugReport
from app.db.models.user import User
from app.deps import get_db
from tests.test_reference_original_cache import referenced_cache as referenced_cache


@pytest.fixture
async def committed_feedback_world(referenced_cache, app_module, monkeypatch):
    owner = referenced_cache
    created = {
        model: set() for model in (BugReport, BugAttachmentUpload, AuditArchiveOutput)
    }
    async with owner.factory() as db:
        users = [
            await db.get(User, user.id)
            for user in (owner.actor, owner.stranger, owner.manager)
        ]

        @event.listens_for(db.sync_session, "after_flush")
        def remember(_session, _context):
            for row in _session.new:
                if type(row) in created and row.id is not None:
                    created[type(row)].add(row.id)

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
                for model in (BugAttachmentUpload, BugReport, AuditArchiveOutput):
                    if created[model]:
                        await db.execute(
                            delete(model).where(model.id.in_(created[model]))
                        )
                await db.commit()
