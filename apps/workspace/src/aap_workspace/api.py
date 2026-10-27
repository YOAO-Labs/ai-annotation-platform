"""Private Workspace HTTP assembly; public admission stays with the caller."""

from __future__ import annotations

import asyncio
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import RuntimeSettings
from .contract import (
    MembershipInvalidRequest,
    MembershipUnavailable,
    decode_membership_request,
)
from .manager import MembershipManager
from .postgres import MembershipPostgresData
from .owner_transfer import OwnerTransferManager
from .owner_transfer_contract import (
    OwnerTransferInvalidRequest,
    OwnerTransferUnavailable,
    decode_owner_transfer_request,
)
from .owner_transfer_postgres import OwnerTransferPostgresData
from .configuration import ConfigurationManager
from .configuration_contract import (
    ConfigurationInvalidRequest,
    ConfigurationUnavailable,
    decode_configuration_request,
)
from .configuration_postgres import ConfigurationPostgresData
from .template_queries import TemplateQueryManager
from .template_query_contract import (
    TemplateQueryInvalidRequest,
    TemplateQueryUnavailable,
    decode_template_query_request,
)
from .template_query_postgres import TemplateQueryPostgresData
from .project_queries import ProjectQueryManager
from aap_workspace_contract.project_query_contract import (
    ProjectQueryInvalidRequest,
    ProjectQueryUnavailable,
    decode_project_query_request,
)
from .project_query_postgres import ProjectQueryPostgresData
from .project_query_profiles import ProjectQueryIdentityProfiles
from .directory_queries import DirectoryQueryManager
from .directory_query_contract import (
    DirectoryQueryInvalidRequest,
    DirectoryQueryUnavailable,
    decode_directory_query_request,
)
from .directory_query_postgres import DirectoryQueryPostgresData


def create_app(
    settings: RuntimeSettings | None = None,
    *,
    data=None,
    owner_transfer_data=None,
    configuration_data=None,
    template_query_data=None,
    project_query_data=None,
    project_query_profiles=None,
    directory_query_data=None,
    directory_query_profiles=None,
) -> FastAPI:
    settings = settings or RuntimeSettings()
    backend_probe_client = settings.inference_probe_client()
    control = settings.control_token().encode("utf-8")
    transfer_dsn = settings.transfer_database_url()
    config_dsn = settings.config_database_url()
    query_dsn = settings.query_database_url()
    project_dsn = settings.project_database_url()
    directory_dsn = settings.directory_database_url()
    if owner_transfer_data is None and transfer_dsn:
        owner_transfer_data = OwnerTransferPostgresData(
            transfer_dsn, command_timeout=settings.timeout_seconds
        )
    if configuration_data is None and config_dsn:
        configuration_data = ConfigurationPostgresData(
            config_dsn, command_timeout=settings.timeout_seconds
        )
    if template_query_data is None and query_dsn:
        template_query_data = TemplateQueryPostgresData(
            query_dsn, command_timeout=settings.timeout_seconds
        )
    if project_query_data is None and project_dsn:
        project_query_data = ProjectQueryPostgresData(
            project_dsn, command_timeout=settings.timeout_seconds
        )
    if project_query_profiles is None:
        project_query_profiles = ProjectQueryIdentityProfiles(settings)
    if directory_query_data is None and directory_dsn:
        directory_query_data = DirectoryQueryPostgresData(
            directory_dsn, command_timeout=settings.timeout_seconds
        )
    if directory_query_profiles is None:
        directory_query_profiles = ProjectQueryIdentityProfiles(settings)
    if data is None and (
        settings.membership_database_url
        or settings.membership_database_url_file
        or (
            owner_transfer_data is None
            and configuration_data is None
            and template_query_data is None
            and project_query_data is None
            and directory_query_data is None
        )
    ):
        data = MembershipPostgresData(
            settings.database_url(), command_timeout=settings.timeout_seconds
        )
    gate = asyncio.Semaphore(settings.max_concurrency)
    app = FastAPI(
        title="Workspace owner",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with gate:
                    if data is not None:
                        await data.membership_policy()
                    if owner_transfer_data is not None:
                        await owner_transfer_data.owner_transfer_policy()
                    if configuration_data is not None:
                        await configuration_data.configuration_policy()
                    if template_query_data is not None:
                        await template_query_data.template_query_policy()
                    if project_query_data is not None:
                        await project_query_data.project_query_policy()
                    if directory_query_data is not None:
                        await directory_query_data.directory_query_policy()
            return {"status": "ready"}
        except (
            MembershipUnavailable,
            OwnerTransferUnavailable,
            ConfigurationUnavailable,
            TemplateQueryUnavailable,
            ProjectQueryUnavailable,
            DirectoryQueryUnavailable,
            TimeoutError,
        ):
            return JSONResponse({"detail": "Workspace unavailable"}, status_code=503)

    def register_command(path, decoder, manager, binding, invalid, unavailable, label):
        async def execute(request: Request):
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer ") or not hmac.compare_digest(
                authorization[7:].encode("utf-8"), control
            ):
                return JSONResponse(
                    {"detail": "Invalid internal credential"}, status_code=401
                )
            if (
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                != "application/json"
            ):
                return JSONResponse(
                    {"detail": "Expected application/json"}, status_code=400
                )
            try:
                command = decoder(await request.body())
            except (
                invalid,
                unavailable,
                ValueError,
                TypeError,
                UnicodeError,
                RecursionError,
            ):
                return JSONResponse(
                    {"detail": f"Invalid Workspace {label} request"}, status_code=400
                )
            try:
                if binding is None:
                    raise unavailable()
                async with asyncio.timeout(settings.timeout_seconds):
                    async with gate:
                        reply = await manager(binding).execute(command)
                return JSONResponse(reply.model_dump(mode="json", by_alias=True))
            except (unavailable, TimeoutError):
                return JSONResponse(
                    {"detail": f"Workspace {label} unavailable"}, status_code=503
                )

        app.add_api_route(path, execute, methods=["POST"])

    register_command(
        "/v1/workspace/membership/execute",
        decode_membership_request,
        MembershipManager,
        data,
        MembershipInvalidRequest,
        MembershipUnavailable,
        "membership",
    )
    register_command(
        "/v1/workspace/owner-transfer/execute",
        decode_owner_transfer_request,
        OwnerTransferManager,
        owner_transfer_data,
        OwnerTransferInvalidRequest,
        OwnerTransferUnavailable,
        "owner transfer",
    )

    def configuration_manager(binding):
        from aap_inference_contract.capabilities import CapabilityLimits

        async def noop_projection(request):
            if project_query_data is None or project_query_profiles is None:
                raise ProjectQueryUnavailable()
            return await ProjectQueryManager(
                project_query_data, project_query_profiles
            ).execute(request)

        return ConfigurationManager(
            binding,
            setup_timeout=settings.configuration_setup_timeout_seconds,
            video_segment_size_frames=settings.configuration_video_segment_size_frames,
            capability_limits=CapabilityLimits(
                video_tracker_window_size_frames=(
                    settings.configuration_video_tracker_window_size_frames
                ),
                video_tracker_sam3_window_size_frames=(
                    settings.configuration_video_tracker_sam3_window_size_frames
                ),
            ),
            backend_probe=(
                backend_probe_client.probe_modalities
                if backend_probe_client is not None
                else None
            ),
            noop_projection=noop_projection,
        )

    register_command(
        "/v1/workspace/configuration/execute",
        decode_configuration_request,
        configuration_manager,
        configuration_data,
        ConfigurationInvalidRequest,
        ConfigurationUnavailable,
        "configuration",
    )
    register_command(
        "/v1/workspace/template-queries/execute",
        decode_template_query_request,
        TemplateQueryManager,
        template_query_data,
        TemplateQueryInvalidRequest,
        TemplateQueryUnavailable,
        "template query",
    )
    register_command(
        "/v1/workspace/project-queries/execute",
        decode_project_query_request,
        lambda binding: ProjectQueryManager(binding, project_query_profiles),
        project_query_data,
        ProjectQueryInvalidRequest,
        ProjectQueryUnavailable,
        "Project query",
    )
    register_command(
        "/v1/workspace/directory-queries/execute",
        decode_directory_query_request,
        lambda binding: DirectoryQueryManager(binding, directory_query_profiles),
        directory_query_data,
        DirectoryQueryInvalidRequest,
        DirectoryQueryUnavailable,
        "directory query",
    )
    return app
