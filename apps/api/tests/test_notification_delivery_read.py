"""Real SQLAlchemy loader checks for Notifications borrowed sessions."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import Uuid, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from aap_notifications.delivery_read_postgres import (
    NotificationDeliveryReadPostgresData,
)


@pytest.mark.asyncio
async def test_fixed_read_loader_preserves_uuid_columns_identity_and_autoflush():
    class Base(DeclarativeBase):
        pass

    class Row(Base):
        __tablename__ = "notification_read_projection"

        id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
        user_id: Mapped[uuid.UUID] = mapped_column(Uuid)
        target_id: Mapped[uuid.UUID] = mapped_column(Uuid)

    notification_id, actor_id, project_id = (uuid.uuid4() for _ in range(3))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    statements = []

    @event.listens_for(engine, "before_cursor_execute", retval=True)
    def substitute_functions(connection, cursor, statement, parameters, context, many):
        if "delivery_member_pairs" in statement:
            statements.append(("member", json.loads(parameters[0])))
            return (
                f"SELECT '{project_id.hex}' AS project_id, '{actor_id.hex}' AS user_id",
                (),
            )
        if "delivery_reviewer_pairs" in statement:
            statements.append(("reviewer", json.loads(parameters[0])))
            return (
                f"SELECT '{project_id.hex}' AS project_id, '{actor_id.hex}' AS user_id",
                (),
            )
        if "read_notification" in statement:
            return (
                f"SELECT '{notification_id.hex}' AS id, '{actor_id.hex}' AS user_id, "
                f"'{project_id.hex}' AS target_id",
                (),
            )
        if "find_terminal_notification" in statement:
            return f"SELECT '{notification_id.hex}' AS find_terminal_notification", ()
        return statement, parameters

    session = Session(engine)

    class BorrowedSession:
        @property
        def autoflush(self):
            return session.autoflush

        async def flush(self):
            session.flush()

        async def execute(self, statement, params=None):
            return session.execute(statement, params)

        async def scalar(self, statement, params=None):
            return session.scalar(statement, params)

    try:
        data = NotificationDeliveryReadPostgresData(BorrowedSession(), Row)
        row = Row(id=notification_id, user_id=actor_id, target_id=project_id)
        session.add(row)
        pairs = {(project_id, actor_id)}
        assert await data.member_pairs(pairs) == pairs
        assert row not in session.new

        dirty_actor = uuid.uuid4()
        row.user_id = dirty_actor
        pending = Row(id=uuid.uuid4(), user_id=actor_id, target_id=project_id)
        session.add(pending)
        with session.no_autoflush:
            assert await data.reviewer_pairs(pairs) == pairs
            loaded = await data.read_notification(notification_id)
            assert loaded is row and loaded.user_id == dirty_actor
            assert row in session.dirty and pending in session.new
            assert (
                await data.find_terminal_notification(
                    actor_id, "job.completed", project_id
                )
                == notification_id
            )
        assert [kind for kind, _pairs in statements] == ["member", "reviewer"]
        assert all(
            pair == [{"project_id": str(project_id), "user_id": str(actor_id)}]
            for _kind, pair in statements
        )
        session.rollback()
        assert session.get(Row, notification_id) is None
    finally:
        session.close()
        engine.dispose()
