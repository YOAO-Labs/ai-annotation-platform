"""Admit an exact fixture original through the Media owner and read-only target."""

from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from functools import partial

from sqlalchemy import text
from yoao_assets_media import contract as media
from yoao_assets_media_runtime.additional_original import (
    AdditionalOriginalTarget,
    load_additional_original_config,
    physical_identity,
)
from yoao_assets_media_runtime.api import error_status
from yoao_assets_media_runtime.original_read import original_permit

from app.services import storage_access
from app.services.assets_media import media_gateway
from app.services.content_snapshot import ContentError


async def is_fixture(db, source):
    """Classify only canonical saved Item originals, never migration routes."""
    if source.owner_kind != "dataset_asset" or source.read_route is not None:
        return False
    owned = await db.scalar(
        text(
            "SELECT aap_assets_v1.is_fixture_original_source("
            "CAST(:key AS text), CAST(:identity AS text))"
        ),
        {
            "key": source.object_key,
            "identity": physical_identity(source.endpoint_url, source.bucket),
        },
    )
    if owned is not True and owned is not False:
        raise ContentError("content_binding_changed")
    return owned


@contextmanager
def _client(config, permit):
    target = AdditionalOriginalTarget(
        config,
        region="us-east-1",
        connect_timeout=10,
        read_timeout=30,
        max_attempts=1,
    )
    try:
        if not target.matches(permit):
            raise ContentError("managed_original_endpoint_changed")
        yield target.client
    finally:
        storage_access.close_registered(target)


@asynccontextmanager
async def admitted(db, source, actor_id, spec):
    """Yield a version-bound read while the durable Media permit remains open.

    The gateway owns independent transactions; this helper never commits the
    caller's session. Callers recheck their own authority before physical I/O.
    """
    try:
        async with media_gateway(db) as database:
            current = await database.call(
                "read_media_source",
                media.ReadSourceRequest(
                    actor_id=actor_id, dataset_item_id=source.item_id
                ),
            )
            async with original_permit(
                database,
                "begin_media_original_actor",
                media.ActorOriginalReadRequest(
                    actor_id=actor_id,
                    dataset_item_id=source.item_id,
                    source_snapshot=current.source_snapshot,
                    object_key=source.object_key,
                ),
                "finish_media_original_read",
            ) as permit:
                if permit.ownership != "fixture":
                    raise ContentError("content_binding_changed")
                config = load_additional_original_config()
                if (
                    config is None
                    or source.replica_facts is None
                    or permit.creation_id != source.replica_facts.creation_id
                    or permit.location_id != source.location_id
                    or permit.endpoint_url != source.endpoint_url
                    or permit.bucket != source.bucket
                    or permit.object_key != source.object_key
                    or permit.sha256 != source.sha256
                    or permit.size_bytes != source.size_bytes
                    or config.endpoint != permit.endpoint_url
                    or config.bucket != permit.bucket
                    or physical_identity(config.endpoint, config.bucket)
                    != permit.storage_identity
                ):
                    raise ContentError("content_binding_changed")
                yield (
                    replace(
                        spec,
                        expected_version_id=permit.version_id,
                        expected_etag=permit.etag,
                    ),
                    {"client_context": partial(_client, config, permit)},
                )
    except media.MediaFailure as exc:
        failure = ContentError(exc.code, error_status(exc.code))
        if getattr(exc, "storage_operation_unsettled", False):
            storage_access.mark_unsettled(failure)
        raise failure from None
