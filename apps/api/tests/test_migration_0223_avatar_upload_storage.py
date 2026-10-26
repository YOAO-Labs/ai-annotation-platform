"""Database-only avatar migration checks in a disposable, fixture-owned schema."""

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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import settings
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_original_retirement import managed_storage_identity


class _HistoricalBase(DeclarativeBase):
    pass


class AvatarUpload(_HistoricalBase):
    """The isolated schema carries the 0223 shape, not the current ORM.

    ``object_version_id`` arrives in 0243, so the application model cannot write
    to this historical table; this mapping mirrors the migration it exercises.
    """

    __tablename__ = "avatar_uploads"

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    storage_ref: Mapped[dict] = mapped_column(JSONB(none_as_null=True), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def migration(connection):
    path = Path(__file__).parents[1] / "alembic/versions/0223_avatar_upload_storage.py"
    spec = importlib.util.spec_from_file_location(
        "avatar_storage_migration_" + uuid.uuid4().hex, path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def assert_historical_columns(sync):
    actual = {column["name"] for column in inspect(sync).get_columns("avatar_uploads")}
    assert actual == set(AvatarUpload.__table__.columns.keys())


@pytest.fixture
async def owned_db(test_engine):
    schema = "avatar_upload_storage_test_" + uuid.uuid4().hex
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


def reference(token):
    endpoint = f"{'https' if settings.minio_use_ssl else 'http'}://{settings.minio_endpoint}".rstrip(
        "/"
    )
    return {
        "schema": 1,
        "purpose": "avatars",
        "location_id": str(LEGACY_LOCATION_ID),
        "config_revision": 1,
        "binding_generation": 1,
        "endpoint_url": endpoint,
        "bucket": "avatars-original",
        "object_key": f"{token}.webp",
        "storage_identity": managed_storage_identity(endpoint, "avatars-original"),
    }


async def create(db, **changes):
    token = uuid.uuid4().hex
    row = AvatarUpload(
        **{
            "token": token,
            "owner_id": uuid.uuid4(),
            "storage_ref": reference(token),
            "sha256": "a" * 64,
            "size_bytes": 101,
            **changes,
        }
    )
    db.add(row)
    await db.flush()
    return row


async def test_empty_downgrade_upgrade_roundtrip_keeps_shared_guard(owned_db):
    connection = await owned_db.connection()

    def roundtrip(sync):
        module = migration(sync)
        assert module.down_revision == "0222"
        schema = sync.scalar(text("SELECT current_schema()"))
        assert inspect(sync).get_foreign_keys("avatar_uploads", schema=schema) == []
        shared_before = sync.scalar(
            text(
                "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
            )
        )
        module.downgrade()
        assert not inspect(sync).has_table("avatar_uploads", schema=schema)
        assert (
            sync.scalar(
                text(
                    "SELECT pg_get_functiondef('public.guard_training_dataset_storage_ref()'::regprocedure)"
                )
            )
            == shared_before
        )
        module.upgrade()
        assert inspect(sync).has_table("avatar_uploads", schema=schema)

    await connection.run_sync(roundtrip)
    row = await create(owned_db)
    assert row.state == "pending"
    assert row.created_at is not None
    assert row.retired_at is None


@pytest.mark.parametrize(
    "field", ["token", "owner_id", "sha256", "size_bytes", "created_at"]
)
async def test_identity_and_independent_content_receipt_are_immutable(owned_db, field):
    row = await create(owned_db)
    changed = {
        "token": uuid.uuid4().hex,
        "owner_id": uuid.uuid4(),
        "sha256": "b" * 64,
        "size_bytes": 102,
        "created_at": row.created_at + timedelta(seconds=1),
    }[field]
    token = row.token
    with pytest.raises(
        IntegrityError, match="identity and content receipt are immutable"
    ):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
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
        ref["object_key"] = f"{uuid.uuid4().hex}.webp"
    elif change == "endpoint":
        ref["endpoint_url"] = "https://other.invalid"
    else:
        ref = None
    token = row.token
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(storage_ref=ref)
            )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", True),
        ("schema", "1"),
        ("schema", 1.0),
        ("purpose", "training-run"),
        ("location_id", str(uuid.uuid4())),
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
        ("object_key", f"{uuid.uuid4().hex}.webp"),
        ("object_key", "../avatar.webp"),
        ("storage_identity", "A" * 64),
        ("storage_identity", None),
        ("credential", "forbidden"),
    ],
)
async def test_insert_requires_strict_locator_shape_and_exact_token_key(
    owned_db, field, value
):
    token = uuid.uuid4().hex
    ref = {**reference(token), field: value}
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, token=token, storage_ref=ref)


@pytest.mark.parametrize("bad", [None, {}, [], "text", True])
async def test_insert_rejects_json_null_or_missing_envelope(owned_db, bad):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await owned_db.execute(
                text(
                    "INSERT INTO avatar_uploads (token,owner_id,storage_ref,sha256,size_bytes) "
                    "VALUES (:token,:owner,CAST(:ref AS jsonb),:sha,101)"
                ),
                {
                    "token": uuid.uuid4().hex,
                    "owner": uuid.uuid4(),
                    "ref": json.dumps(bad),
                    "sha": "a" * 64,
                },
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "A" * 64},
        {"sha256": "a" * 63},
        {"size_bytes": 0},
        {"size_bytes": 2097153},
        {"state": "unknown"},
        {"state": "retired"},
        {"retired_at": datetime.now(timezone.utc)},
    ],
)
async def test_insert_requires_bounded_receipt_and_consistent_lifecycle(
    owned_db, changes
):
    with pytest.raises(IntegrityError):
        async with owned_db.begin_nested():
            await create(owned_db, **changes)


@pytest.mark.parametrize("first_ready", [False, True])
async def test_forward_lifecycle_and_cleanup_clock_are_mutable(owned_db, first_ready):
    row = await create(owned_db)
    if first_ready:
        row.state = "ready"
        await owned_db.flush()
    row.state = "retired"
    row.retired_at = datetime.now(timezone.utc)
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
        owned_db,
        state=initial,
        retired_at=datetime.now(timezone.utc) if initial == "retired" else None,
    )
    token = row.token
    with pytest.raises(IntegrityError, match="state cannot move backwards"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(state=target, retired_at=None)
            )


async def test_retirement_fact_cannot_be_rewritten(owned_db):
    row = await create(owned_db, state="retired", retired_at=datetime.now(timezone.utc))
    token, timestamp = row.token, row.retired_at
    with pytest.raises(IntegrityError, match="retirement is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(retired_at=timestamp + timedelta(seconds=1))
            )


async def test_controlled_restore_changes_only_physical_bucket_and_identity(owned_db):
    row = await create(owned_db)
    token, original = row.token, deepcopy(row.storage_ref)
    mapped = {
        **original,
        "bucket": "avatars-restored",
        "storage_identity": managed_storage_identity(
            original["endpoint_url"], "avatars-restored"
        ),
    }
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='on'"))
    await owned_db.execute(
        update(AvatarUpload)
        .where(AvatarUpload.token == token)
        .values(storage_ref=mapped)
    )
    await owned_db.refresh(row)
    assert row.storage_ref == mapped
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(
                    storage_ref={
                        **mapped,
                        "bucket": "trap",
                        "storage_identity": "b" * 64,
                        "object_key": "b" * 32 + ".webp",
                    }
                )
            )
    with pytest.raises(
        IntegrityError, match="identity and content receipt are immutable"
    ):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(sha256="b" * 64)
            )
    await owned_db.execute(text("SET LOCAL aap.restore_training_storage='off'"))
    with pytest.raises(IntegrityError, match="ownership is immutable"):
        async with owned_db.begin_nested():
            await owned_db.execute(
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(storage_ref=original)
            )


async def test_runtime_role_cannot_forge_restore_marker(owned_db):
    row = await create(owned_db)
    token, original = row.token, deepcopy(row.storage_ref)
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
        text(f'GRANT SELECT,UPDATE ON "{schema}".avatar_uploads TO {quoted}')
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
                update(AvatarUpload)
                .where(AvatarUpload.token == token)
                .values(storage_ref=mapped)
            )
    assert await owned_db.scalar(text("SELECT current_user")) == current


async def test_any_saved_upload_blocks_destructive_downgrade(owned_db):
    await create(owned_db)
    connection = await owned_db.connection()
    with pytest.raises(RuntimeError, match="prevent destructive downgrade"):
        await connection.run_sync(lambda sync: migration(sync).downgrade())
