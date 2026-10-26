"""Attachment ownership migration checks in a disposable, fixture-owned schema."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import DateTime, Integer, String, func, inspect, text, update
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity

CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _HistoricalBase(DeclarativeBase):
    pass


class BugAttachmentUpload(_HistoricalBase):
    """The isolated schema carries the 0225 shape, not the current ORM.

    ``object_version_id`` arrives in 0245, so the application model cannot write
    to this historical table; this mapping mirrors the migration it exercises.
    """

    __tablename__ = "bug_attachment_uploads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(10), nullable=False)
    file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bound_report_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


def migration(connection):
    path = Path(__file__).parents[1] / "alembic/versions/0225_bug_attachment_storage.py"
    spec = importlib.util.spec_from_file_location(
        "bug_attachment_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def assert_historical_columns(sync):
    actual = {
        column["name"] for column in inspect(sync).get_columns("bug_attachment_uploads")
    }
    assert actual == set(BugAttachmentUpload.__table__.columns.keys())


@pytest.fixture
async def owned_db(test_engine):
    schema = "bug_attachment_storage_test_" + uuid.uuid4().hex
    async with test_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        test_engine.url,
        connect_args={"server_settings": {"search_path": schema + ",public"}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync: migration(sync).upgrade())
            await connection.run_sync(assert_historical_columns)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            yield db
    finally:
        await engine.dispose()
        async with test_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def reference(upload_id, owner_id, mime_type="image/png"):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    suffix = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(
        mime_type, "png"
    )
    return {
        "schema": 1,
        "purpose": "bug-reports",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "attachments-original",
        "object_key": f"bug-report-attachments/{owner_id}/{upload_id}.{suffix}",
        "storage_identity": managed_storage_identity(endpoint, "attachments-original"),
    }


async def create(db, **changes):
    values = {
        "id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "mime_type": "image/png",
        "file_name": "screen.png",
        "sha256": "a" * 64,
        "size_bytes": 101,
        "created_at": CREATED,
        "expires_at": CREATED + timedelta(days=180),
        **changes,
    }
    if "storage_ref" not in values:
        values["storage_ref"] = reference(
            values["id"], values["owner_id"], values["mime_type"]
        )
    row = BugAttachmentUpload(**values)
    db.add(row)
    await db.flush()
    return row


async def test_empty_roundtrip_preserves_shared_guard_and_snapshot_ownership(owned_db):
    connection = await owned_db.connection()

    def roundtrip(sync):
        module = migration(sync)
        assert module.down_revision == "0224"
        schema = sync.scalar(text("SELECT current_schema()"))
        assert (
            inspect(sync).get_foreign_keys("bug_attachment_uploads", schema=schema)
            == []
        )
        shared_before = sync.scalar(
            text(
                "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
            )
        )
        module.downgrade()
        assert not inspect(sync).has_table("bug_attachment_uploads", schema=schema)
        assert (
            sync.scalar(
                text(
                    "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
                )
            )
            == shared_before
        )
        module.upgrade()
        assert inspect(sync).has_table("bug_attachment_uploads", schema=schema)

    await connection.run_sync(roundtrip)
    row = await create(owned_db)
    assert row.state == "pending"
    assert row.retired_at is None
    assert row.bound_report_id is None
    assert row.expires_at - row.created_at == timedelta(days=180)


@pytest.mark.parametrize(
    "field",
    [
        "id",
        "owner_id",
        "mime_type",
        "file_name",
        "sha256",
        "size_bytes",
        "created_at",
        "expires_at",
    ],
)
async def test_all_identity_content_and_deadline_facts_are_immutable(owned_db, field):
    row = await create(owned_db)
    changed = {
        "id": uuid.uuid4(),
        "owner_id": uuid.uuid4(),
        "mime_type": "image/webp",
        "file_name": "other.png",
        "sha256": "b" * 64,
        "size_bytes": 102,
        "created_at": row.created_at + timedelta(seconds=1),
        "expires_at": row.expires_at - timedelta(seconds=1),
    }[field]
    upload_id = row.id
    with pytest.raises(
        IntegrityError, match="identity, content and deadline are immutable"
    ):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values({field: changed})
            )


@pytest.mark.parametrize("change", ["bucket", "identity", "key", "endpoint", "clear"])
async def test_saved_owner_refuses_ordinary_rewrite(owned_db, change):
    row = await create(owned_db)
    ref = deepcopy(row.storage_ref)
    if change == "bucket":
        ref["bucket"] = "different"
    elif change == "identity":
        ref["storage_identity"] = "0" * 64
    elif change == "key":
        ref["object_key"] = ref["object_key"].replace(str(row.id), str(uuid.uuid4()))
    elif change == "endpoint":
        ref["endpoint_url"] = "https://other.invalid"
    else:
        ref = None
    upload_id = row.id
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(storage_ref=ref)
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", "1"),
        ("schema", 1.0),
        ("purpose", "training-run"),
        ("location_id", str(uuid.UUID(int=1))),
        ("config_revision", 2),
        ("config_revision", True),
        ("config_revision", 1.0),
        ("binding_generation", "1"),
        ("binding_generation", 1.0),
        ("endpoint_url", "http://user:secret@storage.invalid"),
        ("endpoint_url", "http://storage.invalid/path"),
        ("endpoint_url", "http://storage.invalid\\path"),
        ("bucket", ""),
        ("bucket", "bad/path"),
        ("bucket", "bad\\path"),
        ("bucket", "bad bucket"),
        ("object_key", "../attachment.png"),
        ("storage_identity", "A" * 64),
        ("storage_identity", None),
        ("credential", "forbidden"),
    ],
)
async def test_insert_requires_strict_envelope_and_exact_upload_key(
    owned_db, field, value
):
    upload_id, owner_id = uuid.uuid4(), uuid.uuid4()
    ref = {**reference(upload_id, owner_id), field: value}
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(
                owned_db,
                id=upload_id,
                owner_id=owner_id,
                storage_ref=ref,
            )


@pytest.mark.parametrize("bad", [None, {}, [], "text", True])
async def test_insert_rejects_json_null_or_missing_envelope(owned_db, bad):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "INSERT INTO bug_attachment_uploads (id,owner_id,mime_type,file_name,storage_ref,sha256,size_bytes,created_at,expires_at) "
                    "VALUES (:id,:owner,'image/png','screen.png',CAST(:ref AS jsonb),:sha,101,:created,:expires)"
                ),
                {
                    "id": uuid.uuid4(),
                    "owner": uuid.uuid4(),
                    "ref": json.dumps(bad),
                    "sha": "a" * 64,
                    "created": CREATED,
                    "expires": CREATED + timedelta(days=180),
                },
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"mime_type": "image/jpg"},
        {"file_name": ""},
        {"file_name": "../screen.png"},
        {"file_name": "screen\n.png"},
        {"file_name": "screen\x85.png"},
        {"file_name": "x" * 256},
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"size_bytes": 0},
        {"size_bytes": 10 * 1024**2 + 1},
        {"state": "unknown"},
        {"state": "retired"},
        {"retired_at": CREATED},
        {"expires_at": CREATED},
        {"expires_at": CREATED + timedelta(days=180, seconds=1)},
    ],
)
async def test_insert_requires_receipt_retention_and_consistent_lifecycle(
    owned_db, changes
):
    expected = (
        DBAPIError
        if changes.get("file_name") == "x" * 256
        else (IntegrityError, DataError)
    )
    with pytest.raises(expected) as rejected:
        async with owned_db.begin_nested():
            await create(owned_db, **changes)
    if changes.get("file_name") == "x" * 256:
        assert rejected.value.orig.sqlstate == "22001"


@pytest.mark.parametrize("first_ready", [False, True])
async def test_forward_lifecycle_and_cleanup_clock_are_mutable(owned_db, first_ready):
    row = await create(owned_db)
    if first_ready:
        row.state = "ready"
        await owned_db.flush()
    row.state, row.retired_at = "retired", datetime.now(timezone.utc)
    await owned_db.flush()
    retired_at = row.retired_at
    row.last_attempted_at = datetime.now(timezone.utc)
    await owned_db.flush()
    await owned_db.refresh(row)
    assert row.state == "retired"
    assert row.retired_at == retired_at
    assert row.last_attempted_at is not None


@pytest.mark.parametrize(
    ("initial", "target"),
    [("ready", "pending"), ("retired", "pending"), ("retired", "ready")],
)
async def test_lifecycle_cannot_move_backwards(owned_db, initial, target):
    row = await create(
        owned_db, state=initial, retired_at=CREATED if initial == "retired" else None
    )
    upload_id = row.id
    with pytest.raises(IntegrityError, match="state cannot move backwards"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(state=target, retired_at=None)
            )


async def test_retirement_fact_cannot_be_rewritten(owned_db):
    row = await create(owned_db, state="retired", retired_at=CREATED)
    upload_id = row.id
    with pytest.raises(IntegrityError, match="retirement is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(retired_at=CREATED + timedelta(seconds=1))
            )


async def test_controlled_restore_changes_only_bucket_and_identity(owned_db):
    row = await create(owned_db)
    upload_id, original = row.id, deepcopy(row.storage_ref)
    mapped = {
        **original,
        "bucket": "attachments-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "attachments-restored"
        ),
    }
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(BugAttachmentUpload)
        .where(BugAttachmentUpload.id == upload_id)
        .values(storage_ref=mapped)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == mapped
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(storage_ref={**mapped, "endpoint_url": "https://trap.invalid"})
            )
    for changes in (
        {"sha256": "b" * 64},
        {"expires_at": CREATED + timedelta(days=179)},
    ):
        with pytest.raises(
            IntegrityError, match="identity, content and deadline are immutable"
        ):
            async with owned_db.begin_nested():
                await owned_db.execute(
                    update(BugAttachmentUpload)
                    .where(BugAttachmentUpload.id == upload_id)
                    .values(**changes)
                )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    row = await create(owned_db)
    upload_id, original = row.id, deepcopy(row.storage_ref)
    schema = await owned_db.scalar(text("SELECT current_schema()"))
    current = await owned_db.scalar(text("SELECT current_user"))
    role = make_url(settings.database_url).username
    if role == current:
        pytest.skip("This configured test target has no distinct runtime role")
    if not await owned_db.scalar(
        text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"),
        {"role": role},
    ):
        pytest.skip("Configured runtime role does not exist in this test server")
    quoted = '"' + role.replace('"', '""') + '"'
    await owned_db.execute(text(f'GRANT USAGE ON SCHEMA "{schema}" TO {quoted}'))
    await owned_db.execute(
        text(f'GRANT SELECT,UPDATE ON "{schema}".bug_attachment_uploads TO {quoted}')
    )
    mapped = {
        **original,
        "bucket": "runtime-trap",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "runtime-trap"
        ),
    }
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(text(f"SET LOCAL ROLE {quoted}"))
            await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(storage_ref=mapped)
            )
    assert await owned_db.scalar(text("SELECT current_user")) == current


async def test_any_saved_attachment_blocks_destructive_downgrade(owned_db):
    await create(owned_db)
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())


@pytest.mark.parametrize("mime_type", ["image/png", "image/jpeg", "image/webp"])
async def test_each_supported_mime_owns_its_exact_extension(owned_db, mime_type):
    row = await create(owned_db, mime_type=mime_type, file_name="截图 原件.png")
    assert row.storage_ref == reference(row.id, row.owner_id, mime_type)


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "pending", "bound_report_id": uuid.uuid4()},
        {"file_name": "."},
        {"file_name": ".."},
        {"file_name": "folder\\screen.png"},
        {"file_name": "screen\x7f.png"},
        {"file_name": "screen\x9f.png"},
    ],
)
async def test_initial_pending_and_file_name_are_safe(owned_db, changes):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, **changes)


async def test_ready_upload_binds_once_and_keeps_snapshot_after_retirement(owned_db):
    row = await create(owned_db)
    row.state = "ready"
    await owned_db.flush()
    report_id = uuid.uuid4()
    row.bound_report_id = report_id
    await owned_db.flush()
    row.last_attempted_at = CREATED + timedelta(seconds=1)
    await owned_db.flush()
    row.state, row.retired_at = "retired", CREATED + timedelta(days=1)
    await owned_db.flush()
    await owned_db.refresh(row)
    assert row.bound_report_id == report_id


@pytest.mark.parametrize("replacement", [None, uuid.uuid4()])
async def test_bound_upload_cannot_be_released_or_rebound(owned_db, replacement):
    row = await create(owned_db, state="ready")
    row.bound_report_id = uuid.uuid4()
    await owned_db.flush()
    upload_id = row.id
    with pytest.raises(IntegrityError, match="binding requires first use"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(bound_report_id=replacement)
            )


@pytest.mark.parametrize(
    ("initial", "target"),
    [
        ("pending", "pending"),
        ("pending", "ready"),
        ("pending", "retired"),
        ("ready", "retired"),
        ("retired", "retired"),
    ],
)
async def test_binding_requires_ready_to_ready_transition(owned_db, initial, target):
    row = await create(
        owned_db, state=initial, retired_at=CREATED if initial == "retired" else None
    )
    upload_id = row.id
    with pytest.raises(IntegrityError, match="binding requires first use"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(BugAttachmentUpload)
                .where(BugAttachmentUpload.id == upload_id)
                .values(
                    state=target,
                    bound_report_id=uuid.uuid4(),
                    retired_at=CREATED if target == "retired" else None,
                )
            )
