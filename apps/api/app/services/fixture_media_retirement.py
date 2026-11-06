"""Retire exact fixture media through the native owner and physical receipts."""

from __future__ import annotations

import asyncio
import builtins
from contextlib import asynccontextmanager
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
import re
import sys
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import storage_access
from app.services.content_snapshot import ContentError
from app.services.fixture_media_execution import _control_url


async def _physical(call, *args):
    try:
        pending = asyncio.get_running_loop().run_in_executor(
            None, copy_context().run, partial(call, *args)
        )
    except BaseException as exc:
        raise storage_access.mark_unsettled(exc)
    try:
        return await asyncio.shield(pending)
    except BaseException as interrupted:
        try:
            await storage_access.finish(pending)
        except BaseException as late:
            if storage_access.failure_unsettled(late) and late is not interrupted:
                if isinstance(interrupted, asyncio.CancelledError):
                    storage_access.mark_unsettled(interrupted)
                    raise interrupted from late
                raise builtins.BaseExceptionGroup(
                    "Fixture media removal and native drain failed", [interrupted, late]
                ) from None
        raise


async def _retire_output(db, control, kind, identifier, model, remove):
    from app.services import media_cache_storage

    manifest = await control.output(kind, identifier)
    reference = manifest.get("storage_ref")
    if manifest.get("kind") != kind or manifest.get("id") != str(identifier):
        raise ContentError("fixture_media_owner_changed", 409)
    versions = manifest.get("versions")
    if not isinstance(versions, list):
        raise ContentError("fixture_media_version_unavailable", 503)
    try:
        version_ids = [uuid.UUID(value) for value in versions]
    except (TypeError, ValueError):
        raise ContentError("fixture_media_version_unavailable", 503) from None
    factory = async_sessionmaker(storage_access.engine(db), expire_on_commit=False)
    async with factory() as owner:
        current = await owner.get(model, identifier)
        if (
            current is None
            or current.state != manifest.get("state")
            or current.storage_ref != reference
        ):
            raise ContentError("fixture_media_owner_changed", 409)
        if current.state == "deleted":
            await owner.rollback()
            if not await control.output_done(kind, identifier):
                raise ContentError("fixture_media_output_unsettled", 409)
            return
        if current.state not in {"building", "ready", "failed", "retiring"}:
            raise ContentError("fixture_media_owner_changed", 409)
        state = current.state
        await owner.rollback()
        if state != "retiring":
            await control.transition(kind, identifier, state, "retiring")
        if version_ids:
            saved = media_cache_storage.resolve_ref(reference)
            await storage_access.ensure(owner, saved.location_id)
            target = await media_cache_storage.target_for_ref(owner, reference, require_enabled=False)
            from dataclasses import replace
            from app.core.crypto import encrypt_secret
            from app.services.source_readers.s3 import S3Access

            cleanup = control.cleanup_target
            if (
                target._access is None or target.endpoint_url != cleanup["endpoint"]
                or target.bucket != cleanup["bucket"]
                or reference["storage_identity"] != cleanup["storage_identity"]
                or cleanup["run_id"] != str(control.run_id)
            ):
                raise ContentError("fixture_media_owner_changed", 409)
            target = replace(
                target,
                _access=S3Access(
                    target._access.config,
                    encrypt_secret({"access_key": cleanup["access_key"], "secret_key": cleanup["secret_key"]}),
                    target._access.allowlist,
                ),
            )
            await owner.commit()
            permit = await control.acquire(kind, identifier)
            async with permit:
                current = await owner.get(model, identifier, with_for_update=True, populate_existing=True)
                if (
                    current is None or current.state != "retiring"
                    or current.storage_ref != reference or permit.location_id != saved.location_id
                ):
                    raise ContentError("fixture_media_owner_changed", 409)
                await storage_access.check(
                    owner, saved.location_id, expected_revision=permit.revision, lock=True
                )
                await owner.commit()
                receipts = [await control.version(version_id, permit) for version_id in version_ids]
                await _physical(remove, target, current, (control, permit, receipts))
        if not await control.output_done(kind, identifier):
            raise ContentError("fixture_media_output_unsettled", 409)
        await control.transition(kind, identifier, "retiring", "deleted")


class Retirement:
    def __init__(self, db, connection, run_id, dataset_id, token):
        self.db, self.connection = db, connection
        self.run_id, self.dataset_id, self.token = run_id, dataset_id, token
        self.loop = asyncio.get_running_loop()

    def _scope(self, **extra):
        return {"run": self.run_id, "dataset": self.dataset_id, "token": self.token, **extra}

    async def retire_independent_publications(self):
        value = await self.connection.scalar(
            text("SELECT aap_assets_v1.retire_fixture_independent_publications(:run,:dataset,:token)"),
            self._scope(),
        )
        if value is not True:
            raise ContentError("fixture_media_publication_unsettled", 409)
        await self.connection.commit()

    async def inventory(self):
        value = await self.connection.scalar(
            text("SELECT aap_assets_v1.fixture_media_retirement_inventory(:run,:dataset,:token)"),
            self._scope(),
        )
        if not isinstance(value, list):
            raise ContentError("fixture_media_retirement_unavailable", 503)
        await self.connection.commit()
        return value

    async def output(self, kind, identifier):
        value = await self.connection.scalar(
            text("SELECT aap_assets_v1.fixture_media_output_inventory(:run,:dataset,:token,:kind,:id)"),
            self._scope(kind=kind, id=identifier),
        )
        if not isinstance(value, dict):
            raise ContentError("fixture_media_retirement_unavailable", 503)
        await self.connection.commit()
        return value

    async def acquire(self, kind, identifier):
        operation_id = uuid.uuid4()
        value = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.acquire_fixture_media_retirement_permit("
                ":run,:dataset,:token,:kind,:id,:operation)"
            ),
            self._scope(kind=kind, id=identifier, operation=operation_id),
        )
        if not isinstance(value, dict) or value.get("id") != str(operation_id):
            raise ContentError("fixture_media_retirement_unavailable", 503)
        try:
            location_id, revision = uuid.UUID(value["location_id"]), value["revision"]
        except (KeyError, ValueError, TypeError):
            raise ContentError("fixture_media_retirement_unavailable", 503) from None
        if type(revision) is not int or revision < 1:
            raise ContentError("fixture_media_retirement_unavailable", 503)
        await self.connection.commit()
        return storage_access.Permit(storage_access.engine(self.db), operation_id, location_id, revision)

    async def version(self, identifier, permit):
        value = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.prepare_fixture_media_version_retirement("
                ":run,:dataset,:token,:id,:operation)"
            ),
            self._scope(id=identifier, operation=permit.id),
        )
        if not isinstance(value, dict):
            raise ContentError("fixture_media_version_unavailable", 503)
        await self.connection.commit()
        return value

    async def advance(self, identifier, permit, state):
        accepted = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.advance_fixture_media_version_retirement("
                ":run,:dataset,:token,:id,:operation,:state)"
            ),
            self._scope(id=identifier, operation=permit.id, state=state),
        )
        if accepted is not True:
            raise ContentError("fixture_media_version_unavailable", 503)
        await self.connection.commit()

    async def output_done(self, kind, identifier):
        done = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.fixture_media_output_retirement_done("
                ":run,:dataset,:token,:kind,:id)"
            ),
            self._scope(kind=kind, id=identifier),
        )
        await self.connection.commit()
        return done is True

    async def transition(self, kind, identifier, expected, next_state):
        changed = await self.connection.scalar(
            text(
                "SELECT aap_assets_v1.transition_fixture_media_output("
                ":run,:dataset,:token,:kind,:id,:expected,:next)"
            ),
            self._scope(kind=kind, id=identifier, expected=expected, next=next_state),
        )
        if changed is not True:
            await self.connection.rollback()
            raise ContentError("fixture_media_owner_changed", 409)
        await self.connection.commit()

    async def native_inventory(self):
        value = await self.connection.scalar(
            text("SELECT aap_assets_v1.fixture_native_retirement_inventory(:run,:dataset,:token)"),
            self._scope(),
        )
        if not isinstance(value, dict):
            raise ContentError("fixture_native_retirement_unavailable", 503)
        await self.connection.commit()
        return value

    async def native_scope(self, prefix, *, finish=False):
        name = "finish_fixture_native_scope" if finish else "prepare_fixture_native_scope"
        result = await self.connection.scalar(
            text(f"SELECT aap_assets_v1.{name}(:run,:dataset,:token,:prefix)"),
            self._scope(prefix=prefix),
        )
        if result is not True:
            raise ContentError("fixture_native_retirement_unavailable", 503)
        await self.connection.commit()

    async def native_version(self, identifier):
        value = await self.connection.scalar(
            text("SELECT aap_assets_v1.prepare_fixture_native_retirement(:run,:dataset,:token,:id)"),
            self._scope(id=identifier),
        )
        if not isinstance(value, dict) or value.get("id") != str(identifier):
            raise ContentError("fixture_native_retirement_unavailable", 503)
        await self.connection.commit()
        return value

    async def native_advance(self, identifier, state):
        accepted = await self.connection.scalar(
            text("SELECT aap_assets_v1.advance_fixture_native_retirement(:run,:dataset,:token,:id,:state)"),
            self._scope(id=identifier, state=state),
        )
        if accepted is not True:
            raise ContentError("fixture_native_retirement_unavailable", 503)
        await self.connection.commit()

    async def retire_native_jobs(self):
        rows = await self.connection.scalar(
            text("SELECT aap_assets_v1.fixture_native_job_inventory(:run,:dataset,:token)"),
            self._scope(),
        )
        if not isinstance(rows, list):
            raise ContentError("fixture_native_retirement_unavailable", 503)
        await self.connection.commit()
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"family", "job_id"} or row["family"] not in {"image", "media"}:
                raise ContentError("fixture_native_owner_changed", 409)
            try:
                identifier = uuid.UUID(row["job_id"])
            except (TypeError, ValueError, AttributeError):
                raise ContentError("fixture_native_owner_changed", 409) from None
            identity = (row["family"], identifier)
            if identity in seen:
                raise ContentError("fixture_native_owner_changed", 409)
            seen.add(identity)
            retired = await self.connection.scalar(
                text("SELECT aap_assets_v1.retire_fixture_native_job(:run,:dataset,:token,:family,:id)"),
                self._scope(family=row["family"], id=identifier),
            )
            if retired is not True:
                raise ContentError("fixture_native_retirement_unavailable", 503)
            await self.connection.commit()

    def native_checkpoint(self, identifier, state):
        coroutine = self.native_advance(uuid.UUID(identifier), state)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException as exc:
            coroutine.close()
            raise storage_access.mark_unsettled(exc)
        try:
            return future.result()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)

    def physical_checkpoint(self, identifier, permit, state):
        coroutine = self.advance(identifier, permit, state)
        try:
            future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        except BaseException as exc:
            coroutine.close()
            raise storage_access.mark_unsettled(exc)
        try:
            return future.result()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)


@asynccontextmanager
async def retirement(db, run_id, dataset_id, control_url):
    """Hold the native session until this owner's physical work really closes."""
    engine = create_async_engine(_control_url(control_url), poolclass=NullPool, hide_parameters=True)
    failure = None
    try:
        async with engine.connect() as connection:
            acquired = await connection.scalar(
                text(
                    "SELECT pg_catalog.pg_try_advisory_lock("
                    "pg_catalog.hashtextextended(:label,0))"
                ),
                {"label": f"aap-fixture-media:retirement:{dataset_id}"},
            )
            await connection.commit()
            if acquired is not True:
                raise ContentError("fixture_media_retirement_busy", 409)
            token = uuid.uuid4()
            begun = await connection.scalar(
                text("SELECT aap_jobs_v1.begin_fixture_media_retirement(:run,:dataset,:token)"),
                {"run": run_id, "dataset": dataset_id, "token": token},
            )
            if begun is not True:
                await connection.rollback()
                raise ContentError("fixture_media_retirement_unsettled", 409)
            await connection.commit()
            try:
                yield Retirement(db, connection, run_id, dataset_id, token)
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if not storage_access.failure_unsettled(failure):
                    try:
                        finished = await connection.scalar(
                            text("SELECT aap_jobs_v1.finish_fixture_media_retirement(:run,:dataset,:token)"),
                            {"run": run_id, "dataset": dataset_id, "token": token},
                        )
                        if finished is not True:
                            raise ContentError("fixture_media_retirement_unsettled", 409)
                        await connection.commit()
                    except BaseException as finish_error:
                        storage_access.mark_unsettled(finish_error)
                        if isinstance(failure, asyncio.CancelledError):
                            storage_access.mark_unsettled(failure)
                            raise failure from finish_error
                        if failure is not None:
                            raise builtins.BaseExceptionGroup(
                                "Fixture media retirement and settlement failed", [failure, finish_error]
                            ) from None
                        raise
    finally:
        primary = sys.exception()
        try:
            await engine.dispose()
        except BaseException as close_error:
            storage_access.mark_unsettled(close_error)
            if isinstance(primary, asyncio.CancelledError):
                storage_access.mark_unsettled(primary)
                raise primary from close_error
            if primary is not None:
                raise builtins.BaseExceptionGroup(
                    "Fixture media retirement and shutdown failed", [primary, close_error]
                ) from None
            raise


_NATIVE_SCOPE = re.compile(
    r"^(?:image-assets|image-pyramids|media-assets)/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/$"
)


@dataclass(frozen=True)
class _NativeOwner:
    bucket: str
    prefix: str
    ref: dict

    def contains(self, key):
        if (
            not isinstance(key, str)
            or not key.startswith(self.prefix)
            or not key[len(self.prefix):]
            or "\\" in key
            or any(part in {"", ".", ".."} for part in key.split("/"))
        ):
            raise ContentError("fixture_native_owner_changed", 409)


def _remove_native_scope(target, owner, receipts, checkpoint):
    import boto3
    from botocore.config import Config
    from app.services import media_cache_storage

    client = boto3.client(
        "s3", endpoint_url=target["endpoint"], region_name="us-east-1",
        aws_access_key_id=target["access_key"], aws_secret_access_key=target["secret_key"],
        config=Config(
            signature_version="s3v4", connect_timeout=3, read_timeout=30,
            retries={"mode": "standard", "total_max_attempts": 1},
            s3={"addressing_style": "path"}, proxies={},
        ),
    )
    try:
        return media_cache_storage.retire_fixture_versions(client, owner, receipts, checkpoint)
    finally:
        primary = sys.exception()
        try:
            client.close()
        except BaseException as close_error:
            storage_access.mark_unsettled(close_error)
            if primary is not None:
                combined = builtins.BaseExceptionGroup(
                    "Native output retirement and client close failed", [primary, close_error]
                )
                storage_access.mark_unsettled(combined)
                raise combined from None
            raise


async def _retire_native_outputs(control, control_url, cleanup_file):
    from yoao_assets_media_runtime.fixture_native_outputs import load_target_file

    target = load_target_file(
        cleanup_file, control_url.render_as_string(hide_password=False), "aap_fixture_media_"
    )
    manifest = await control.native_inventory()
    if set(manifest) != {"storage_ref", "scopes"} or not isinstance(manifest["scopes"], list):
        raise ContentError("fixture_native_owner_changed", 409)
    reference = {key: target[key] for key in (
        "schema", "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256"
    )}
    if manifest["storage_ref"] != reference or target["run_id"] != str(control.run_id):
        raise ContentError("fixture_native_owner_changed", 409)
    seen_scopes, seen_writes = set(), set()
    for entry in manifest["scopes"]:
        if (
            not isinstance(entry, dict) or set(entry) != {"prefix", "write_ids"}
            or not isinstance(entry["prefix"], str)
            or _NATIVE_SCOPE.fullmatch(entry["prefix"]) is None
            or entry["prefix"] in seen_scopes
            or not isinstance(entry["write_ids"], list)
        ):
            raise ContentError("fixture_native_owner_changed", 409)
        seen_scopes.add(entry["prefix"])
        try:
            identifiers = [uuid.UUID(value) for value in entry["write_ids"]]
        except (ValueError, TypeError, AttributeError):
            raise ContentError("fixture_native_owner_changed", 409) from None
        if len(set(identifiers)) != len(identifiers) or seen_writes.intersection(identifiers):
            raise ContentError("fixture_native_owner_changed", 409)
        seen_writes.update(identifiers)
        await control.native_scope(entry["prefix"])
        receipts = [await control.native_version(identifier) for identifier in identifiers]
        owner = _NativeOwner(target["bucket"], entry["prefix"], reference)
        await _physical(_remove_native_scope, target, owner, receipts, control.native_checkpoint)
        await control.native_scope(entry["prefix"], finish=True)


async def retire_dataset(db, run_id, dataset_id, control_url, *, native_cleanup_file):
    """Coordinate only the Dataset's born native owners, retaining their receipts."""
    from app.services import (
        image_pyramid_objects,
        thumbnail_cleanup,
        video_derivative_objects,
    )

    owners = {
        "thumbnail": thumbnail_cleanup.retire_fixture,
        "pyramid_output": image_pyramid_objects.retire_fixture,
        "video_output": video_derivative_objects.retire_fixture,
    }
    from yoao_assets_media_runtime.fixture_native_outputs import load_target_file

    cleanup = load_target_file(
        native_cleanup_file, control_url.render_as_string(hide_password=False), "aap_fixture_media_"
    )
    if cleanup["run_id"] != str(run_id):
        raise ContentError("fixture_native_owner_changed", 409)
    async with retirement(db, run_id, dataset_id, control_url) as control:
        await control.retire_independent_publications()
        native_manifest = await control.native_inventory()
        if native_manifest.get("storage_ref") != {
            key: cleanup[key] for key in (
                "schema", "run_id", "endpoint", "bucket", "storage_identity", "receipt_sha256",
            )
        }:
            raise ContentError("fixture_native_owner_changed", 409)
        control.cleanup_target = cleanup
        for item in await control.inventory():
            if not isinstance(item, dict) or set(item) != {"kind", "id", "item_id"}:
                raise ContentError("fixture_media_owner_changed", 409)
            remove = owners.get(item["kind"])
            try:
                identifier = uuid.UUID(item["id"])
            except (TypeError, ValueError):
                raise ContentError("fixture_media_owner_changed", 409) from None
            if remove is None:
                raise ContentError("fixture_media_owner_changed", 409)
            await remove(db, control, identifier)
        await _retire_native_outputs(control, control_url, native_cleanup_file)
        await control.retire_native_jobs()
        complete = await control.connection.scalar(
            text("SELECT aap_assets_v1.fixture_media_retirement_complete(:run,:dataset,:token)"),
            {"run": run_id, "dataset": dataset_id, "token": control.token},
        )
        await control.connection.commit()
        if complete is not True:
            raise ContentError("fixture_media_retirement_unsettled", 409)
