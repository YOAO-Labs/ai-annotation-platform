"""Launch independent Inference capabilities with capability-scoped credentials."""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path

from aap_inference_contract.capabilities import CapabilityLimits

from .backend_probe_manager import BackendProbeManager
from .backend_probe_postgres import BackendProbePostgresData
from .health_observation_manager import BackendHealthObservationManager
from .health_observation_postgres import BackendHealthObservationPostgresData
from .pipeline_manager import PipelineManager
from .pipeline_postgres import PipelinePostgresData
from .pipeline_server import PipelineHttpServer
from .registry_ingress_manager import RegistryEnvSyncManager
from .routing_runtime import RoutingRuntime


def _environment_value(name: str) -> str | None:
    direct = os.environ.get(name) or None
    file_path = os.environ.get(f"{name}_FILE") or None
    if direct is not None and file_path is not None:
        raise ValueError(f"{name} and {name}_FILE cannot both be set")
    if file_path is not None:
        try:
            return Path(file_path).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise ValueError(f"Unable to read {name}_FILE") from None
    return direct


def _required_value(name: str) -> str:
    value = _environment_value(name)
    if not value:
        raise ValueError(f"{name} or {name}_FILE is required")
    return value


def _integer_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not raw.isascii() or not raw.isdigit():
        raise ValueError(f"Invalid {name}")
    significant = raw.lstrip("0") or "0"
    if len(significant) > len(str(maximum)):
        raise ValueError(f"Invalid {name}")
    value = int(significant)
    if not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def _boolean_setting(name: str, default: bool = False) -> bool:
    from pydantic import TypeAdapter, ValidationError

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return TypeAdapter(bool).validate_python(raw)
    except ValidationError:
        raise ValueError(f"Invalid {name}") from None


@dataclass(frozen=True)
class _BackendOptions:
    ml_health_timeout: int
    ml_predict_timeout: int


class _SystemClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    async def sleep(delay: float) -> None:
        await asyncio.sleep(delay)


async def _gpu_runtime(
    stack, routing_runtime, options, limits, *, max_concurrency, admin
):
    import asyncpg

    from .gpu.backend_dispatch import BackendGPUDispatchContextFactory
    from .gpu.collector_database import open_gpu_collector_database
    from .gpu.dispatch import build_gpu_dispatch_context_factory
    from .gpu.endpoint_source import GPUOwnedBackendEndpointSource
    from .gpu.ledger import GPUArbiterStore, GPUArbiterStoreError
    from .gpu.manager import build_gpu_authority_manager
    from .gpu.native import GPUAuthorityPoolData
    from .gpu.settings import GPUArbiterMode, GPUAuthoritySettings, parse_gpu_resources
    from .gpu.signing import GPUAdmissionTokenSigner
    from .gpu_health import PersistedGPUHealthRefresher
    from .owner_transport import BackendTransportGPUControlAdapter

    resources, errors = parse_gpu_resources(
        os.environ.get("GPU_ARBITER_RESOURCES_JSON", "{}")
    )
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode(os.environ.get("GPU_ARBITER_MODE", "off")),
        gpu_arbiter_resources=resources,
        gpu_arbiter_config_errors=errors,
        gpu_arbiter_rollout_enabled=_boolean_setting("GPU_ARBITER_ROLLOUT_ENABLED"),
        gpu_arbiter_admission_timeout_seconds=_integer_setting(
            "GPU_ARBITER_ADMISSION_TIMEOUT_SECONDS", 30, 1, 3600
        ),
        gpu_arbiter_residency_cooldown_seconds=_integer_setting(
            "GPU_ARBITER_RESIDENCY_COOLDOWN_SECONDS", 30, 1, 3600
        ),
        ml_predict_timeout=options.ml_predict_timeout,
        redis_url=_environment_value("INFERENCE_GPU_REDIS_URL") or "",
        gpu_lifecycle_signing_keys_file=os.environ.get(
            "GPU_LIFECYCLE_SIGNING_KEYS_FILE", ""
        ),
        gpu_lifecycle_active_signing_kid=os.environ.get(
            "GPU_LIFECYCLE_ACTIVE_SIGNING_KID", ""
        ),
        gpu_arbiter_collector_database_url_file=os.environ.get(
            "GPU_ARBITER_COLLECTOR_DATABASE_URL_FILE", ""
        ),
    )

    async def pool_for(name, application_name):
        pool = await asyncpg.create_pool(
            dsn=_required_value(name).replace(
                "postgresql+asyncpg://", "postgresql://", 1
            ),
            min_size=0,
            max_size=max_concurrency,
            timeout=5,
            command_timeout=30,
            server_settings={"application_name": application_name},
        )
        stack.push_async_callback(pool.close)
        return pool

    authority_pool = await pool_for("INFERENCE_GPU_DATABASE_URL", "aap-inference-gpu")
    data = GPUAuthorityPoolData(authority_pool)
    endpoint_source = GPUOwnedBackendEndpointSource(
        {
            purpose: await pool_for(
                f"INFERENCE_GPU_ENDPOINT_{purpose.upper()}_DATABASE_URL",
                f"aap-inference-gpu-endpoint-{purpose}",
            )
            for purpose in ("health_observation", "gpu_control")
        }
    )
    transport = BackendTransportGPUControlAdapter(routing_runtime)
    clock = _SystemClock()

    def store_factory():
        return GPUArbiterStore.from_url(config.redis_url)

    def signer_factory():
        return GPUAdmissionTokenSigner.from_settings(config)

    health_refresher = PersistedGPUHealthRefresher(
        _required_value("INFERENCE_HEALTH_DATA_DATABASE_URL"),
        options=options,
        capability_limits_factory=lambda: limits,
    )
    dispatch = build_gpu_dispatch_context_factory(
        data,
        endpoint_source,
        transport,
        config=config,
        store_factory=store_factory,
        signer_factory=signer_factory,
        health_refresher=health_refresher,
        clock=clock,
    )
    boundary = BackendGPUDispatchContextFactory(
        data, config=config, authority_dispatch_factory=dispatch, clock=clock
    )
    manager = None
    if admin:

        async def application_role_probe():
            async with authority_pool.acquire() as connection:
                return await connection.fetchval("SELECT current_user")

        collector = await open_gpu_collector_database(application_role_probe, config)
        stack.push_async_callback(collector.aclose)

        async def demote_resource(resource_id):
            resource = config.gpu_arbiter_resources.get(resource_id)
            if resource is None:
                raise GPUArbiterStoreError("GPU resource configuration is unavailable")
            async with store_factory() as store:
                result = await store.mark_card_not_ready(
                    resource_id,
                    resource.allocatable_mb,
                    reason="membership_promotion_in_progress",
                )
                if result.status != "not_ready":
                    raise GPUArbiterStoreError(
                        "GPU readiness demotion was not confirmed"
                    )

        manager = build_gpu_authority_manager(
            dispatch,
            data=data,
            collector_data=collector,
            store_factory=store_factory,
            signer_factory=signer_factory,
            endpoint_source=endpoint_source,
            backend_transport=transport,
            config=config,
            readiness_demoter=demote_resource,
            clock=clock,
            max_dispatch_duration_seconds=max(
                120,
                options.ml_predict_timeout
                + config.gpu_arbiter_admission_timeout_seconds
                + 30,
            ),
        )
        stack.push_async_callback(manager.aclose)
    return boundary, manager


async def run() -> None:
    dsn = _environment_value("INFERENCE_PIPELINES_DATABASE_URL")
    token = _environment_value("INFERENCE_CONTROL_TOKEN")
    probe_dsn = _environment_value("INFERENCE_BACKEND_PROBE_DATABASE_URL")
    probe_token = _environment_value("INFERENCE_BACKEND_PROBE_CONTROL_TOKEN")
    observation_dsn = _environment_value("INFERENCE_HEALTH_OBSERVATION_DATABASE_URL")
    observation_token = _environment_value("INFERENCE_HEALTH_OBSERVATION_TOKEN")
    registry_dsn = _environment_value("INFERENCE_REGISTRY_INGRESS_DATABASE_URL")
    registry_token = _environment_value("INFERENCE_REGISTRY_INGRESS_TOKEN")
    routing_dsn = _environment_value("INFERENCE_ROUTING_DATABASE_URL")
    routing_token = _environment_value("INFERENCE_ROUTING_TOKEN")
    execution_token = _environment_value("INFERENCE_MODEL_EXECUTION_TOKEN")
    backend_control_token = _environment_value("INFERENCE_BACKEND_CONTROL_TOKEN")
    gpu_token = _environment_value("INFERENCE_GPU_TOKEN")
    for name, database_url, bearer in (
        ("backend probe", probe_dsn, probe_token),
        ("health observation", observation_dsn, observation_token),
        ("registry ingress", registry_dsn, registry_token),
        ("routing", routing_dsn, routing_token),
    ):
        if (database_url is None) != (bearer is None):
            raise ValueError(
                f"Inference {name} database and bearer must be configured together"
            )
    if any((execution_token, backend_control_token, gpu_token)) and not (
        routing_dsn and routing_token
    ):
        raise ValueError(
            "Inference execution/control/GPU requires the routing capability"
        )
    host = os.environ.get("INFERENCE_HOST", "127.0.0.1")
    if not host or any(character.isspace() for character in host):
        raise ValueError("Invalid INFERENCE_HOST")
    port = _integer_setting("INFERENCE_PORT", 8768, 1, 65_535)
    max_concurrency = _integer_setting("INFERENCE_MAX_CONCURRENCY", 8, 1, 64)
    required_value = os.environ.get("INFERENCE_REQUIRED_CAPABILITIES", "")
    required = tuple(required_value.split(",")) if required_value else ()
    options = _BackendOptions(
        ml_health_timeout=int(os.environ.get("ML_HEALTH_TIMEOUT", "10")),
        ml_predict_timeout=int(os.environ.get("ML_PREDICT_TIMEOUT", "100")),
    )
    limits = CapabilityLimits(
        video_tracker_window_size_frames=int(
            os.environ.get("VIDEO_TRACKER_WINDOW_SIZE_FRAMES", "300")
        ),
        video_tracker_sam3_window_size_frames=int(
            os.environ.get("VIDEO_TRACKER_SAM3_WINDOW_SIZE_FRAMES", "16")
        ),
    )
    async with AsyncExitStack() as stack:
        data = manager = probe_manager = observation_manager = registry_manager = (
            routing
        ) = None
        if dsn and token:
            data = PipelinePostgresData(dsn)
            stack.push_async_callback(data.close)
            manager = PipelineManager(data)
        if probe_dsn is not None and probe_token is not None:
            probe_data = BackendProbePostgresData(probe_dsn)
            stack.push_async_callback(probe_data.close)
            probe_manager = BackendProbeManager(probe_data)
        if observation_dsn is not None and observation_token is not None:
            observation_data = BackendHealthObservationPostgresData(observation_dsn)
            stack.push_async_callback(observation_data.close)
            observation_manager = BackendHealthObservationManager(observation_data)
        if registry_dsn is not None and registry_token is not None:
            registry_manager = RegistryEnvSyncManager(registry_dsn)
            stack.push_async_callback(registry_manager.close)
        if routing_dsn is not None and routing_token is not None:
            routing = RoutingRuntime(
                routing_dsn,
                health_max_age_seconds=_integer_setting(
                    "INFERENCE_ROUTING_HEALTH_MAX_AGE_SECONDS", 90, 1, 3600
                ),
                lease_ttl_ms=_integer_setting(
                    "INFERENCE_ROUTING_LEASE_TTL_SECONDS", 120, 1, 3600
                )
                * 1000,
            )
            stack.push_async_callback(routing.close)
        execution = backend_control = gpu_manager = None
        if any((execution_token, backend_control_token, gpu_token)):
            boundary, gpu_manager = await _gpu_runtime(
                stack,
                routing,
                options,
                limits,
                max_concurrency=max_concurrency,
                admin=gpu_token is not None,
            )
            from .backend_endpoint import BackendEndpointManager
            from .backend_endpoint_postgres import BackendEndpointPostgresData
            from .owner_transport import (
                RoutingBackendControlRequestContextFactory,
                RoutingModelRequestContextFactory,
            )

            purposes = (
                (
                    "setup",
                    "health_observation",
                    "catalog_fallback",
                    "stats",
                    "backend_read",
                    "lifecycle",
                    "execution",
                )
                if backend_control_token is not None
                else ("execution",)
                if execution_token is not None
                else ()
            )
            openers = {
                purpose: BackendEndpointManager(
                    BackendEndpointPostgresData(
                        _required_value(
                            f"INFERENCE_BACKEND_ENDPOINT_{purpose.upper()}_DATABASE_URL"
                        ),
                        purpose,
                    )
                )
                for purpose in purposes
            }
            if execution_token is not None:
                from .model_execution import BackendTransportModelExecutionProvider

                execution = BackendTransportModelExecutionProvider(
                    openers["execution"],
                    options=options,
                    request_context_factory=RoutingModelRequestContextFactory(routing),
                    dispatch_context_factory=boundary,
                )
            if backend_control_token is not None:
                from .backend_control_manager import BackendControlManager
                from .backend_control_stats_postgres import (
                    BackendControlStatsPostgresData,
                )

                backend_control = BackendControlManager(
                    openers,
                    options,
                    request_context_factory=RoutingBackendControlRequestContextFactory(
                        routing
                    ),
                    dispatch_context_factory=boundary,
                    unregistered_smoke_dispatch=boundary.unregistered_smoke_dispatch,
                    stats_data=BackendControlStatsPostgresData(
                        _required_value("INFERENCE_BACKEND_STATS_DATABASE_URL")
                    ),
                )
        server = PipelineHttpServer(
            manager,
            data,
            token,
            host=host,
            port=port,
            max_concurrency=max_concurrency,
            backend_probe_manager=probe_manager,
            backend_probe_control_token=probe_token,
            health_observation_manager=observation_manager,
            health_observation_control_token=observation_token,
            registry_ingress_manager=registry_manager,
            registry_ingress_control_token=registry_token,
            routing_manager=routing,
            routing_control_token=routing_token,
            model_execution_manager=execution,
            model_execution_control_token=execution_token,
            backend_control_manager=backend_control,
            backend_control_token=backend_control_token,
            gpu_manager=gpu_manager,
            gpu_control_token=gpu_token,
            required_capabilities=required,
        )
        stack.push_async_callback(server.close)
        await server.serve_forever()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
