"""Qualify one admitted binding candidate; no storage locator enters the wire."""

from uuid import UUID, uuid4

from asyncpg import LockNotAvailableError

from yoao_assets_source.saved_io import SavedIOError

_CAPABILITY = "yoao_assets_v1.capture_saved_binding_probe"


async def _probe(context, subject, body=None):
    if set(subject) != {"operation_id"} or not isinstance(subject["operation_id"], str):
        raise SavedIOError("invalid_request", status_code=400)
    try:
        operation = UUID(subject["operation_id"])
    except ValueError:
        raise SavedIOError("invalid_request", status_code=400) from None
    attempt = uuid4()
    try:
        initial = await context.fetch(_CAPABILITY, operation, attempt, True)
    except LockNotAvailableError:
        raise SavedIOError("probe_busy") from None
    if not isinstance(initial, dict):
        raise SavedIOError("probe_scope_unproven")
    policy = await context.connector_allowlist()
    result = await context.probe(
        initial["location_id"],
        initial["config_revision"],
        initial["connection_revision"],
        initial["purpose"],
        initial["bucket"],
        initial["prefix"],
    )
    try:
        current = await context.fetch(_CAPABILITY, operation, attempt, False)
    except LockNotAvailableError:
        raise SavedIOError("probe_busy") from None
    if current != initial or await context.connector_allowlist() != policy:
        raise SavedIOError("probe_scope_unproven")
    return result


def register(registry):
    registry.allow(f"{_CAPABILITY}(uuid,uuid,boolean)")
    registry.add("binding", "probe", "command", _probe, access="write")
