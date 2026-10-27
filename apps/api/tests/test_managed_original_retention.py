"""Reviewed policy changes preserve group protection and declared history."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from jose import jwt
from pydantic import ValidationError
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import settings
from app.db.models.audit_log import AuditLog
from app.db.models.dataset_asset import StorageBinding
from app.db.models.managed_original_retention_hold import (
    ManagedOriginalRetentionHold as Hold,
)
from app.db.models.managed_original_retirement import (
    ManagedOriginalRetirement as Retirement,
)
from app.db.models.user import User
from app.schemas.managed_original_retention import (
    ManagedOriginalRetentionApply as Apply,
    ManagedOriginalRetentionChange as Change,
)
from app.services import api_key_service, managed_original_retention as service
from app.services.storage import storage_service
from tests.factory import create_user
from tests.test_dataset_content import headers
from tests.test_managed_original_reclamation import assertion, recorded_statements


def future(days=1):
    return datetime.now(timezone.utc) + timedelta(days=days)


def change(*, action="set_retention", revision=1, **fields):
    base = {
        "action": action,
        "expected_revision": revision,
        "reason": "Reviewed requirement",
    }
    if action == "set_retention":
        base["retain_until"] = future()
    elif action == "add_hold":
        base.update(
            hold_id=uuid.uuid4(), hold_kind="backup", reference_code="BACKUP:2026-10-24"
        )
    return Change.model_validate(base | fields)


def confirmed(payload, preview):
    return Apply.model_validate(
        payload.model_dump(exclude_unset=True) | {"impact_token": preview.impact_token}
    )


async def reviewed_apply(db, row, actor, payload):
    preview = await service.preview(db, row.id, payload, actor)
    return await service.apply(db, row.id, confirmed(payload, preview), actor)


@pytest.mark.parametrize(
    "fields",
    [
        {"retain_until": "2030-01-01T12:00:00"},
        {"reason": " \n\t "},
        {"hold_id": str(uuid.uuid4())},
        {"expected_revision": 0},
        {"unknown": "forbidden"},
    ],
)
def test_retention_candidate_rejects_ambiguous_or_irrelevant_fields(fields):
    with pytest.raises(ValidationError):
        Change.model_validate(
            {
                "action": "set_retention",
                "expected_revision": 1,
                "reason": "Reviewed",
                "retain_until": None,
            }
            | fields
        )


def test_each_action_requires_explicit_exact_fields_and_normalizes_utc():
    with pytest.raises(ValidationError):
        Change(action="set_retention", expected_revision=1, reason="Review")
    with pytest.raises(ValidationError):
        change(action="add_hold", retain_until=None)
    with pytest.raises(ValidationError):
        change(action="add_hold", reference_code="https://private.invalid/path")
    with pytest.raises(ValidationError):
        change(action="release_hold", hold_id=uuid.uuid4(), expected_hold_revision=None)
    candidate = change(retain_until="2030-01-01T08:00:00+08:00", reason="  Reviewed  ")
    assert candidate.retain_until == datetime(2030, 1, 1, tzinfo=timezone.utc)
    assert candidate.reason == "Reviewed"


async def test_policy_shortening_indefinite_group_and_atomic_safe_audit(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    peer = await assertion(
        db,
        object_key=row.object_key,
        storage_identity=None,
        identity_state="unknown",
        identity_error_code="unproven",
    )
    provenance = row.assertion_digest
    payload = change()
    preview = await service.preview(db, row.id, payload, actor)
    assert preview.shortens_retention
    assert (
        preview.before.retain_until is None
        and preview.after.retain_until == payload.retain_until
    )
    assert preview.after.indefinite_retention and preview.after.peer_assertions == 2
    commit = AsyncMock(side_effect=AssertionError("service must not commit"))
    monkeypatch.setattr(db, "commit", commit)
    result = await service.apply(db, row.id, confirmed(payload, preview), actor)
    assert result.policy_revision == 2 and result.summary.indefinite_retention
    assert result.summary.retain_until == payload.retain_until
    assert (
        result.coverage == "declared_requirements_only"
        and not result.execution_permission
    )
    assert peer.retain_until is None and row.assertion_digest == provenance
    audit = await db.scalar(select(AuditLog).where(AuditLog.target_id == str(row.id)))
    assert audit.detail_json["previous_revision"] == 1
    serialized = str(audit.detail_json)
    assert row.object_key not in serialized and row.bucket not in serialized
    assert (
        preview.impact_token not in serialized and "Reviewed requirement" in serialized
    )
    commit.assert_not_called()
    reset = await reviewed_apply(db, row, actor, change(revision=2, retain_until=None))
    assert reset.policy_revision == 3 and reset.summary.retain_until is None


async def test_group_uses_latest_finite_deadline(db_session, super_admin):
    db, actor = db_session, super_admin[0]
    row = await assertion(db, retain_until=future(1))
    await assertion(db, object_key=row.object_key, retain_until=future(4))
    current = await service.state(db, row.id, actor)
    assert not current.summary.indefinite_retention
    assert current.summary.effective_retain_until > current.summary.retain_until
    payload = change(retain_until=future(2))
    preview = await service.preview(db, row.id, payload, actor)
    assert not preview.shortens_retention
    assert (
        preview.after.effective_retain_until == current.summary.effective_retain_until
    )


async def test_past_deadline_and_deadline_elapsed_during_confirmation(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    with pytest.raises(HTTPException) as error:
        await service.preview(db, row.id, change(retain_until=future(-1)), actor)
    assert error.value.status_code == 422
    payload = change()
    preview = await service.preview(db, row.id, payload, actor)
    original = service._observe

    async def late_observation(*args, **kwargs):
        observed = dict(await original(*args, **kwargs))
        observed["measured_at"] = payload.retain_until + timedelta(seconds=1)
        return observed

    monkeypatch.setattr(service, "_observe", late_observation)
    with pytest.raises(HTTPException) as error:
        await service.apply(db, row.id, confirmed(payload, preview), actor)
    assert error.value.detail["code"] == "managed_retention_deadline_not_future"
    assert row.policy_revision == 1 and row.retain_until is None


async def test_group_holds_require_exact_anchor_and_preserve_released_history(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    peer = await assertion(db, object_key=row.object_key)
    added = change(action="add_hold")
    await reviewed_apply(db, peer, actor, added)
    current = await service.state(db, row.id, actor)
    assert (
        current.summary.backup_holds == 1 and current.holds[0].retirement_id == peer.id
    )
    hold = await db.get(Hold, added.hold_id)
    original = hold.reason, hold.reference_code, hold.created_by, hold.created_at
    release = change(action="release_hold", hold_id=hold.id, expected_hold_revision=1)
    with pytest.raises(HTTPException) as error:
        await service.preview(db, row.id, release, actor)
    assert error.value.status_code == 404
    release = change(
        action="release_hold",
        revision=2,
        hold_id=hold.id,
        expected_hold_revision=1,
        reason="Recovery obligation reviewed",
    )
    result = await reviewed_apply(db, peer, actor, release)
    assert result.policy_revision == 3 and result.summary.backup_holds == 0
    assert (
        hold.revision == 2
        and hold.released_by == actor.id
        and hold.released_at is not None
    )
    assert (
        hold.reason,
        hold.reference_code,
        hold.created_by,
        hold.created_at,
    ) == original
    assert hold.release_reason == "Recovery obligation reviewed"
    assert result.total_holds == 1 and result.summary.indefinite_retention


async def test_duplicate_hold_id_reference_and_apply_replay_never_duplicate(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    payload = change(action="add_hold")
    preview = await service.preview(db, row.id, payload, actor)
    await service.apply(db, row.id, confirmed(payload, preview), actor)
    with pytest.raises(HTTPException):
        await service.apply(db, row.id, confirmed(payload, preview), actor)
    with pytest.raises(HTTPException) as error:
        await service.preview(
            db,
            row.id,
            change(action="add_hold", revision=2, hold_id=payload.hold_id),
            actor,
        )
    assert error.value.detail["code"] == "managed_retention_hold_id_exists"
    with pytest.raises(HTTPException) as error:
        await service.preview(db, row.id, change(action="add_hold", revision=2), actor)
    assert error.value.detail["code"] == "managed_retention_hold_reference_exists"
    assert await db.scalar(select(func.count()).select_from(Hold)) == 1


async def test_bounded_history_active_first_newest_first(db_session, super_admin):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    records = [
        Hold(
            id=uuid.uuid4(),
            retirement_id=row.id,
            kind="backup",
            reference_code=f"ref-{i}",
            reason="Recorded requirement",
            created_by=actor.id,
            created_at=future(i),
            **(
                {
                    "revision": 2,
                    "released_at": future(i),
                    "released_by": actor.id,
                    "release_reason": "Reviewed",
                }
                if i % 2
                else {}
            ),
        )
        for i in range(5)
    ]
    db.add_all(records)
    await db.flush()
    result = await service.state(db, row.id, actor, limit=2)
    assert [h.id for h in result.holds] == [records[4].id, records[2].id]
    assert (
        result.total_holds == 5 and result.has_more and result.summary.backup_holds == 3
    )
    rest = await service.state(db, row.id, actor, limit=2, offset=2)
    assert [h.id for h in rest.holds] == [records[0].id, records[3].id]


@pytest.mark.parametrize("replace", ["peer", "hold"])
async def test_same_count_replacement_invalidates_review(
    db_session, super_admin, replace
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    peer = await assertion(db, object_key=row.object_key)
    hold = Hold(
        id=uuid.uuid4(),
        retirement_id=row.id,
        kind="migration",
        reference_code="MIGRATION:1",
        reason="Observe",
        created_by=actor.id,
    )
    db.add(hold)
    await db.flush()
    payload = change()
    preview = await service.preview(db, row.id, payload, actor)
    if replace == "peer":
        await db.delete(peer)
        await db.flush()
        await assertion(db, object_key=row.object_key)
    else:
        await db.delete(hold)
        await db.flush()
        db.add(
            Hold(
                id=uuid.uuid4(),
                retirement_id=row.id,
                kind="migration",
                reference_code="MIGRATION:1",
                reason="Observe",
                created_by=actor.id,
            )
        )
        await db.flush()
    with pytest.raises(HTTPException) as error:
        await service.apply(db, row.id, confirmed(payload, preview), actor)
    assert error.value.detail["code"] == "managed_retention_preview_changed"
    assert row.policy_revision == 1


@pytest.mark.parametrize(
    "changed",
    [
        "candidate",
        "reason",
        "expired",
        "tampered",
        "configuration",
        "local_configuration",
        "revision",
    ],
)
async def test_confirmation_binds_candidate_revision_configuration_and_time(
    db_session, super_admin, monkeypatch, changed
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    payload = change()
    preview = await service.preview(db, row.id, payload, actor)
    applied = confirmed(payload, preview)
    if changed == "candidate":
        applied.retain_until = future(2)
    elif changed == "reason":
        applied.reason = "Different review"
    elif changed == "expired":
        decoded = jwt.decode(
            applied.impact_token,
            settings.secret_key,
            algorithms=["HS256"],
            audience=service.AUDIENCE,
        )
        decoded["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        applied.impact_token = jwt.encode(
            decoded, settings.secret_key, algorithm="HS256"
        )
    elif changed == "tampered":
        applied.impact_token = "invalid.signature.token"
    elif changed == "configuration":
        binding = await db.get(StorageBinding, "datasets")
        binding.generation += 1
        await db.flush()
    elif changed == "local_configuration":
        monkeypatch.setattr(service, "configured_identity", lambda: ("changed",))
    else:
        row.policy_revision += 1
        await db.flush()
    with pytest.raises(HTTPException) as error:
        await service.apply(db, row.id, applied, actor)
    assert error.value.status_code == 409
    assert row.retain_until is None


async def test_current_actor_key_and_exact_credential_are_required(
    db_session, super_admin
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    key, _ = await api_key_service.create_key(db, actor, "Full authority", scopes=["*"])
    other, _ = await api_key_service.create_key(
        db, actor, "Other authority", scopes=["*"]
    )
    payload = change()
    preview = await service.preview(db, row.id, payload, actor, api_key_id=key.id)
    with pytest.raises(HTTPException) as error:
        await service.apply(
            db, row.id, confirmed(payload, preview), actor, api_key_id=other.id
        )
    assert error.value.status_code == 409
    key.scopes = ["datasets:write"]
    await db.flush()
    with pytest.raises(HTTPException) as error:
        await service.apply(
            db, row.id, confirmed(payload, preview), actor, api_key_id=key.id
        )
    assert error.value.status_code == 403
    key.scopes = ["*"]
    actor.is_active = False
    await db.flush()
    with pytest.raises(HTTPException) as error:
        await service.apply(
            db, row.id, confirmed(payload, preview), actor, api_key_id=key.id
        )
    assert error.value.status_code == 403


async def test_read_and_preview_are_select_only_and_never_touch_storage(
    db_session, super_admin, monkeypatch
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    commit = AsyncMock(side_effect=AssertionError("unexpected commit"))
    monkeypatch.setattr(db, "commit", commit)
    client = MagicMock()
    client.meta.endpoint_url = storage_service.client.meta.endpoint_url
    monkeypatch.setattr(
        service,
        "storage_service",
        SimpleNamespace(client=client, datasets_bucket=storage_service.datasets_bucket),
    )
    with recorded_statements(db) as statements:
        await service.state(db, row.id, actor)
        await service.preview(db, row.id, change(), actor)
    assert statements and all(
        query.lstrip().upper().startswith(("SELECT", "WITH")) for query in statements
    )
    assert all(
        "FOR UPDATE" not in query.upper() and "ADVISORY" not in query.upper()
        for query in statements
    )
    assert client.mock_calls == []
    commit.assert_not_called()


@pytest.mark.parametrize("action", ["set_retention", "add_hold", "release_hold"])
async def test_audit_failure_rolls_back_entire_change(
    db_session, super_admin, monkeypatch, action
):
    db, actor = db_session, super_admin[0]
    row = await assertion(db)
    if action == "release_hold":
        hold = Hold(
            id=uuid.uuid4(),
            retirement_id=row.id,
            kind="backup",
            reference_code="BACKUP:rollback",
            reason="Retain",
            created_by=actor.id,
        )
        db.add(hold)
        await db.flush()
        payload = change(action=action, hold_id=hold.id, expected_hold_revision=1)
    else:
        payload = change(action=action)
    preview = await service.preview(db, row.id, payload, actor)
    monkeypatch.setattr(
        service.AuditService,
        "log",
        AsyncMock(side_effect=RuntimeError("audit unavailable")),
    )
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await service.apply(db, row.id, confirmed(payload, preview), actor)
    await db.refresh(row)
    assert row.policy_revision == 1 and row.retain_until is None
    if action == "add_hold":
        assert not await db.get(Hold, payload.hold_id)
    elif action == "release_hold":
        await db.refresh(hold)
        assert hold.revision == 1 and hold.released_at is None


@pytest.fixture
async def committed_group(test_engine, monkeypatch):
    # This fixture isolates real cross-session group serialization. Transaction-
    # scoped cases above exercise successful audit writes and audit rollback.
    # Keep immutable audit rows untouched: even deleting their actor would issue
    # an audit UPDATE through its SET NULL FK during committed cleanup.
    monkeypatch.setattr(service.AuditService, "log", AsyncMock(return_value=None))
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with factory() as db:
        actor = await create_user(
            db, "super_admin", f"retention-{uuid.uuid4()}@test.local", "Retention owner"
        )
        row = await assertion(db)
        peer = await assertion(db, object_key=row.object_key)
        await db.commit()
    try:
        yield factory, actor, row, peer
    finally:
        async with factory() as db:
            ids = [row.id, peer.id]
            await db.execute(delete(Hold).where(Hold.retirement_id.in_(ids)))
            await db.execute(delete(Retirement).where(Retirement.id.in_(ids)))
            await db.execute(delete(User).where(User.id == actor.id))
            await db.commit()


async def test_cross_anchor_group_mutations_are_serialized_and_recheck_peers(
    committed_group,
):
    factory, actor, row, peer = committed_group
    async with factory() as left, factory() as right:
        first, second = change(), change()
        first_preview = await service.preview(left, row.id, first, actor)
        second_preview = await service.preview(right, peer.id, second, actor)
        await service.apply(left, row.id, confirmed(first, first_preview), actor)
        with pytest.raises(HTTPException) as error:
            await service.apply(
                right, peer.id, confirmed(second, second_preview), actor
            )
        assert error.value.detail["code"] == "managed_retention_busy"
        await right.rollback()
        await left.commit()
        with pytest.raises(HTTPException) as error:
            await service.apply(
                right, peer.id, confirmed(second, second_preview), actor
            )
        assert error.value.detail["code"] == "managed_retention_preview_changed"


async def test_api_contract_role_scope_validation_and_pagination(
    db_session, super_admin, project_admin, httpx_client
):
    row = await assertion(db_session)
    base = f"/api/v1/storage/managed-original-retirements/{row.id}"
    assert (
        await httpx_client.get(base + "/retention", headers=headers(project_admin))
    ).status_code == 403
    _, raw = await api_key_service.create_key(
        db_session, super_admin[0], "Insufficient scope", scopes=["datasets:write"]
    )
    assert (
        await httpx_client.get(
            base + "/retention", headers={"Authorization": f"Bearer {raw}"}
        )
    ).status_code == 403
    assert (
        await httpx_client.get(
            base + "/retention?limit=101", headers=headers(super_admin)
        )
    ).status_code == 422
    candidate = change()
    preview = await httpx_client.post(
        base + "/retention-preview",
        headers=headers(super_admin),
        json=candidate.model_dump(mode="json", exclude_unset=True),
    )
    assert preview.status_code == 200
    applied = await httpx_client.post(
        base + "/retention",
        headers=headers(super_admin),
        json=candidate.model_dump(mode="json", exclude_unset=True)
        | {"impact_token": preview.json()["impact_token"]},
    )
    assert applied.status_code == 200
    assert (
        applied.json()["policy_revision"] == 2
        and applied.json()["execution_permission"] is False
    )
    assert "object_key" not in applied.text and "storage_identity" not in applied.text
