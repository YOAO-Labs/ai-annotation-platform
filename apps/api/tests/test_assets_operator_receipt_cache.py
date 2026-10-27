"""Receipt reuse boundary: one held caller transaction, released savepoints only."""

from __future__ import annotations

import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.assets_operator import AssetsOperatorGateway


def _session_in_transaction():
    connection = create_engine("sqlite://").connect()
    transaction = connection.begin()
    return Session(bind=connection), connection, transaction


def test_no_active_transaction_never_reuses_or_remembers_receipts():
    connection = create_engine("sqlite://").connect()
    try:
        session = Session(bind=connection)
        gateway = AssetsOperatorGateway(session)
        creation_id = uuid.uuid4()
        digest = "a" * 64

        gateway._remember_receipt(creation_id, digest, 3)
        assert gateway._same_uow_receipt(creation_id) is None
    finally:
        connection.close()


def test_held_transaction_receipts_are_shared_with_a_new_gateway():
    session, connection, transaction = _session_in_transaction()
    try:
        creation_id = uuid.uuid4()
        digest = "a" * 64
        AssetsOperatorGateway(session)._remember_receipt(creation_id, digest, 3)

        replay = AssetsOperatorGateway(session)
        assert replay._same_uow_receipt(creation_id) == (digest, 3)
    finally:
        transaction.rollback()
        connection.close()


def test_session_savepoint_release_keeps_the_replay_receipt():
    session, connection, transaction = _session_in_transaction()
    try:
        creation_id = uuid.uuid4()
        digest = "a" * 64
        gateway = AssetsOperatorGateway(session)
        gateway._remember_receipt(creation_id, digest, 3)

        session.commit()  # releases the caller's savepoint, not the connection

        replay = AssetsOperatorGateway(session)
        assert replay._same_uow_receipt(creation_id) == (digest, 3)
    finally:
        transaction.rollback()
        connection.close()


def test_explicit_savepoint_release_keeps_the_receipt():
    session, connection, transaction = _session_in_transaction()
    try:
        creation_id = uuid.uuid4()
        digest = "a" * 64
        gateway = AssetsOperatorGateway(session)
        savepoint = session.begin_nested()
        gateway._remember_receipt(creation_id, digest, 3)
        savepoint.commit()

        assert gateway._same_uow_receipt(creation_id) == (digest, 3)
        assert AssetsOperatorGateway(session)._same_uow_receipt(creation_id) == (
            digest,
            3,
        )
    finally:
        transaction.rollback()
        connection.close()


def test_rolled_back_savepoint_invalidates_shared_receipts():
    session, connection, transaction = _session_in_transaction()
    try:
        creation_id = uuid.uuid4()
        digest = "a" * 64
        gateway = AssetsOperatorGateway(session)
        savepoint = session.begin_nested()
        gateway._remember_receipt(creation_id, digest, 3)
        assert gateway._same_uow_receipt(creation_id) == (digest, 3)

        savepoint.rollback()

        assert gateway._same_uow_receipt(creation_id) is None
        assert AssetsOperatorGateway(session)._same_uow_receipt(creation_id) is None
    finally:
        transaction.rollback()
        connection.close()


def test_session_rollback_invalidates_receipts():
    session, connection, transaction = _session_in_transaction()
    try:
        creation_id = uuid.uuid4()
        digest = "a" * 64
        gateway = AssetsOperatorGateway(session)
        session.begin_nested()
        gateway._remember_receipt(creation_id, digest, 3)

        session.rollback()

        assert gateway._same_uow_receipt(creation_id) is None
        assert AssetsOperatorGateway(session)._same_uow_receipt(creation_id) is None
    finally:
        transaction.rollback()
        connection.close()


def test_rolled_back_caller_transaction_invalidates_receipts():
    session, connection, transaction = _session_in_transaction()
    creation_id = uuid.uuid4()
    digest = "a" * 64
    gateway = AssetsOperatorGateway(session)
    gateway._remember_receipt(creation_id, digest, 3)
    assert gateway._same_uow_receipt(creation_id) == (digest, 3)

    transaction.rollback()

    assert gateway._same_uow_receipt(creation_id) is None
    connection.close()


def test_committed_caller_transaction_never_reuses_old_receipts():
    session, connection, transaction = _session_in_transaction()
    creation_id = uuid.uuid4()
    digest = "a" * 64
    gateway = AssetsOperatorGateway(session)
    gateway._remember_receipt(creation_id, digest, 3)
    assert gateway._same_uow_receipt(creation_id) == (digest, 3)
    transaction.commit()

    assert gateway._same_uow_receipt(creation_id) is None
    transaction = connection.begin()
    try:
        assert gateway._same_uow_receipt(creation_id) is None
        gateway._remember_receipt(creation_id, digest, 3)
        assert gateway._same_uow_receipt(creation_id) == (digest, 3)
    finally:
        transaction.rollback()
        connection.close()
