"""Exercise real ordinary LOGINs without granting avatar ledger or account tables."""

from contextlib import asynccontextmanager
import hashlib
import os
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import avatar_storage, avatar_upload_data, avatar_uploads
from app.services.managed_original_retirement import managed_storage_identity
from tests._avatar_storage import patch_storage
from tests.factory import DEFAULT_PASSWORD
from tests.test_identity_embedded_acl import embedded_http, seeded_embedded_accounts
from tests.test_me_avatar import AVATAR_URL, _bearer, _png_bytes
from tests.test_workspace_project_authority_locking import _api_url

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the disposable verifier and the real ordinary LOGINs",
    ),
]

_WORKTREE = ("AAP_MODULE_DATA_LEGACY_RUNTIME_URL", "legacy_runtime")
_ROLES = [
    (
        "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
        "identity_embedded_api_consumer",
    ),
    ("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer"),
    _WORKTREE,
]


@asynccontextmanager
async def runtime(test_engine, selector, role):
    verified = _api_url(test_engine)
    url = make_url(os.environ[selector])
    assert (url.host, url.port, url.database, url.username) == (
        verified.host,
        verified.port,
        verified.database,
        role,
    )
    engine = create_async_engine(url, hide_parameters=True)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("selector", "role"), _ROLES, ids=("lan-embedded", "lan-issuer", "worktree")
)
async def test_ordinary_avatar_receipts_use_fixed_owner_commands(
    test_engine, selector, role
):
    token = uuid.uuid4().hex
    async with seeded_embedded_accounts(test_engine) as (actor, inactive):
        owner = async_sessionmaker(test_engine, expire_on_commit=False)
        async with owner.begin() as db:
            await db.execute(
                text("UPDATE public.users SET is_active=false WHERE id=:id"),
                {"id": inactive.id},
            )
            ref = await avatar_storage.allocate(db, token)
        receipt = SimpleNamespace(
            token=token,
            storage_ref=ref,
            sha256=hashlib.sha256(b"webp").hexdigest(),
            size_bytes=4,
            object_version_id=None,
        )
        forged_bucket = ref["bucket"] + "-unrelated"
        forged = SimpleNamespace(**vars(receipt))
        forged.storage_ref = {
            **ref,
            "bucket": forged_bucket,
            "storage_identity": managed_storage_identity(
                ref["endpoint_url"], forged_bucket
            ),
        }
        async with runtime(test_engine, selector, role) as engine:
            try:
                maker = async_sessionmaker(engine)
                async with maker() as db:
                    assert await db.scalar(text("SELECT current_user")) == role
                    for denied in (
                        "SELECT storage_ref FROM public.avatar_uploads WHERE false",
                        "INSERT INTO public.avatar_uploads (token) "
                        "SELECT 'x' WHERE false",
                        "UPDATE public.avatar_uploads "
                        "SET last_attempted_at=now() WHERE false",
                        "DELETE FROM public.avatar_uploads WHERE false",
                        "SELECT password_hash FROM public.users WHERE false",
                        "SELECT key_hash FROM public.api_keys WHERE false",
                        "SELECT yoao_assets_v1.matches_avatar_reservation(NULL, NULL)",
                    ):
                        with pytest.raises(DBAPIError) as error:
                            async with db.begin_nested():
                                await db.execute(text(denied))
                        assert error.value.orig.sqlstate == "42501"
                    reserved = await avatar_upload_data.reserve(
                        db, inactive.id, receipt
                    )
                    assert reserved == "avatar_authority_revoked"
                    reserved = await avatar_upload_data.reserve(db, actor.id, forged)
                    assert reserved == "avatar_storage_binding_unproven"
                    reserved = await avatar_upload_data.reserve(db, actor.id, receipt)
                    assert reserved == "reserved"
                    assert await avatar_upload_data.read_ready(db, token) is None
                    assert not await avatar_upload_data.confirm_version(
                        db, inactive.id, receipt, "provider-v1"
                    )
                    assert await avatar_upload_data.confirm_version(
                        db, actor.id, receipt, "provider-v1"
                    )
                    assert not await avatar_upload_data.confirm_version(
                        db, actor.id, receipt, "provider-v2"
                    )
                    await db.rollback()
                async with owner() as db:
                    assert not await db.scalar(
                        text("SELECT 1 FROM public.avatar_uploads WHERE token=:token"),
                        {"token": token},
                    )
                async with maker.begin() as db:
                    reserved = await avatar_upload_data.reserve(db, actor.id, receipt)
                    assert reserved == "reserved"
                    assert await avatar_upload_data.confirm_version(
                        db, actor.id, receipt, "provider-v1"
                    )
                    receipt.object_version_id = "provider-v1"
                    assert await avatar_upload_data.pending_matches(
                        db, actor.id, receipt
                    )
                    assert await avatar_upload_data.begin_retirement(db, token) is None
                async with owner.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE public.avatar_uploads SET state='retired', "
                            "retired_at=now() WHERE token=:token"
                        ),
                        {"token": token},
                    )
                    await db.execute(
                        text("UPDATE public.users SET avatar_ref=:ref WHERE id=:actor"),
                        {"ref": f"upload:{token}", "actor": actor.id},
                    )
                async with maker.begin() as db:
                    assert token in await avatar_upload_data.list_retired(db, 100)
                    assert await avatar_upload_data.begin_retirement(db, token) is None
                    assert not await avatar_upload_data.finish_retirement(db, receipt)
                async with owner.begin() as db:
                    await db.execute(
                        text("UPDATE public.users SET avatar_ref=NULL WHERE id=:actor"),
                        {"actor": actor.id},
                    )
                    await db.execute(
                        text(
                            "SELECT 1 FROM public.avatar_uploads "
                            "WHERE token=:token FOR UPDATE"
                        ),
                        {"token": token},
                    )
                    async with maker.begin() as contender:
                        assert (
                            await avatar_upload_data.begin_retirement(contender, token)
                            is None
                        )
                async with maker.begin() as db:
                    candidate = await avatar_upload_data.begin_retirement(db, token)
                    assert candidate.storage_ref == ref
                    assert candidate.object_version_id == "provider-v1"
                    assert not await avatar_upload_data.finish_retirement(db, forged)
                    assert await avatar_upload_data.finish_retirement(db, receipt)
                    assert not await avatar_upload_data.finish_retirement(db, receipt)
            finally:
                async with owner.begin() as db:
                    await db.execute(
                        text("DELETE FROM public.avatar_uploads WHERE token=:token"),
                        {"token": token},
                    )


async def test_worktree_avatar_http_replacement_and_retirement(
    test_engine, app_module, monkeypatch
):
    storage = patch_storage(monkeypatch)
    async with (
        seeded_embedded_accounts(test_engine) as (_, actor),
        runtime(test_engine, *_WORKTREE) as engine,
        embedded_http(app_module, engine, monkeypatch) as client,
    ):
        try:
            login = await client.post(
                "/api/v1/auth/login",
                json={"email": actor.email, "password": DEFAULT_PASSWORD},
            )
            assert login.status_code == 200, login.text
            headers = _bearer(login.json()["access_token"])

            async def upload():
                response = await client.post(
                    AVATAR_URL,
                    headers=headers,
                    files={"file": ("avatar.png", _png_bytes((256, 256)), "image/png")},
                )
                assert response.status_code == 200, response.text
                account = response.json()
                assert {
                    "storage_ref",
                    "object_version_id",
                    "key_hash",
                    "secret_enc",
                }.isdisjoint(account)
                return account["avatar_ref"].split(":", 1)[1]

            first = await upload()
            path = f"/api/v1/avatars/{first}"
            public = await client.get(path)
            assert public.status_code == 200
            assert public.headers["content-type"] == "image/webp"
            unchanged = await client.get(
                path, headers={"If-None-Match": public.headers["etag"]}
            )
            assert unchanged.status_code == 304
            storage.fail_deletes = True
            second = await upload()
            assert first != second and (await client.get(path)).status_code == 404
            async with test_engine.connect() as observer:
                states = await observer.execute(
                    text(
                        "SELECT token, state FROM public.avatar_uploads "
                        "WHERE owner_id=:actor"
                    ),
                    {"actor": actor.id},
                )
                pointer = await observer.scalar(
                    text("SELECT avatar_ref FROM public.users WHERE id=:actor"),
                    {"actor": actor.id},
                )
            assert dict(states.all()) == {first: "retired", second: "ready"}
            assert pointer == f"upload:{second}"
            storage.fail_deletes = False
            original_bucket = storage.avatars_bucket
            storage.avatars_bucket += "-changed"
            async with async_sessionmaker(engine)() as db:
                retired = await avatar_uploads.cleanup(db, token=first)
            assert retired == {"deleted": 1, "failed": 0, "retained": 0}
            assert (original_bucket, f"{first}.webp") in storage.deleted
            cleared = await client.delete(AVATAR_URL, headers=headers)
            assert cleared.status_code == 200 and cleared.json()["avatar_ref"] is None
        finally:
            async with test_engine.begin() as db:
                await db.execute(
                    text("DELETE FROM public.avatar_uploads WHERE owner_id=:actor"),
                    {"actor": actor.id},
                )
