from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from yoao_labeling.format_adapter_contract import (
    FormatAdapterPluginRecord,
    FormatAdapterPluginSecret,
)
from yoao_labeling.format_adapter_security import (
    FormatAdapterHttpClient,
    decrypt_adapter_token,
)
from yoao_labeling.format_adapter_store_postgres import (
    FormatAdapterStorePostgresData,
)

from app.config import settings
from app.schemas.mask_format import MaskFormatCapability
from app.services.mask_formats.catalog import registry
from app.services.mask_formats.contracts import (
    MaskFormatAdapter,
    MaskFormatDescriptor,
)
from app.services.mask_formats.remote_adapter import RemoteFormatAdapter


def format_adapter_encryption_key() -> str:
    if settings.format_adapter_encryption_key:
        return settings.format_adapter_encryption_key.strip()
    if settings.format_adapter_encryption_key_file:
        return (
            Path(settings.format_adapter_encryption_key_file)
            .read_text(encoding="utf-8")
            .strip()
        )
    return ""


def build_format_adapter_http_client() -> FormatAdapterHttpClient:
    return FormatAdapterHttpClient(
        host_allowlist=settings.format_adapter_host_allowlist,
        allow_insecure_http_hosts=settings.format_adapter_allow_insecure_http_hosts,
    )


def descriptor_from_plugin(
    plugin: FormatAdapterPluginRecord | FormatAdapterPluginSecret,
) -> MaskFormatDescriptor:
    caps = set(plugin.capabilities)
    return MaskFormatDescriptor(
        format_id=plugin.format_id,
        label=plugin.display_name,
        adapter_version=plugin.adapter_version,
        manifest_version=plugin.manifest_version,
        media_types=frozenset(plugin.media_types),
        import_capability=MaskFormatCapability(
            supported="import" in caps,
            verified=True,
            enabled_for_ui="import" in caps,
        ),
        export_capability=MaskFormatCapability(
            supported="export" in caps,
            verified=True,
            enabled_for_ui="export" in caps,
        ),
        option_schema=dict(plugin.option_schema or {}),
    )


async def list_format_descriptors(
    db: AsyncSession,
    *,
    media_type: str | None = None,
) -> list[MaskFormatDescriptor]:
    descriptors: dict[str, MaskFormatDescriptor] = {
        adapter.descriptor.format_id: adapter.descriptor
        for adapter in registry.list(media_type=media_type)
    }
    plugins = await FormatAdapterStorePostgresData(db).list_plugins(
        include_retired=False
    )
    for plugin in plugins:
        if plugin.status != "active":
            continue
        if media_type is not None and media_type not in plugin.media_types:
            continue
        if plugin.format_id not in descriptors:
            descriptors[plugin.format_id] = descriptor_from_plugin(plugin)
    return [descriptors[key] for key in sorted(descriptors)]


async def resolve_format_adapter(
    db: AsyncSession,
    format_id: str,
) -> MaskFormatAdapter:
    try:
        return registry.get(format_id)
    except ValueError:
        pass
    secret = await FormatAdapterStorePostgresData(db).open_secret(format_id)
    if secret is None or secret.status != "active":
        raise ValueError(f"unsupported format adapter: {format_id}")
    if not secret.auth_token_ciphertext:
        raise ValueError(f"format adapter credentials unavailable: {format_id}")
    auth_token = decrypt_adapter_token(
        secret.auth_token_ciphertext,
        format_adapter_encryption_key(),
    )
    return RemoteFormatAdapter(
        descriptor_from_plugin(secret),
        endpoint_url=secret.endpoint_url,
        auth_token=auth_token,
        client=build_format_adapter_http_client(),
    )
