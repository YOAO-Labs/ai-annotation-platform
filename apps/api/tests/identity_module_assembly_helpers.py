"""Owned Compose assembly helpers for the complete Identity verifier."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
import uuid

import httpx
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from tests.test_identity_admission_live import ROOT, _dsn
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_sessions_live import _verify_redis


def create_retained_realm() -> Path:
    """Keep cold Linux mount sources outside the checkout and automatic cleanup."""
    return Path(tempfile.mkdtemp(prefix="aap-jobs-cold-", dir="/tmp"))


_IDENTITY_DATABASES = {
    "AUTH": ("IDENTITY_RUNTIME_DATABASE_URL_FILE", "identity_database_url"),
    "KEYS": ("IDENTITY_RUNTIME_KEY_DATABASE_URL_FILE", "identity_key_database_url"),
    "SESSIONS": (
        "IDENTITY_RUNTIME_SESSION_DATABASE_URL_FILE",
        "identity_session_database_url",
    ),
    "ACCOUNTS": (
        "IDENTITY_RUNTIME_ACCOUNT_DATABASE_URL_FILE",
        "identity_account_database_url",
    ),
    "PRESENCE": (
        "IDENTITY_RUNTIME_PRESENCE_DATABASE_URL_FILE",
        "identity_presence_database_url",
    ),
    "GROUP": (
        "IDENTITY_RUNTIME_GROUP_DATABASE_URL_FILE",
        "identity_group_database_url",
    ),
    "DIRECTORY": (
        "IDENTITY_RUNTIME_DIRECTORY_DATABASE_URL_FILE",
        "identity_directory_database_url",
    ),
    "ADMIN_PASSWORD": (
        "IDENTITY_RUNTIME_ADMIN_PASSWORD_DATABASE_URL_FILE",
        "identity_admin_password_database_url",
    ),
    "PLATFORM_ROLE": (
        "IDENTITY_RUNTIME_PLATFORM_ROLE_DATABASE_URL_FILE",
        "identity_platform_role_database_url",
    ),
    "SUSPENSION": (
        "IDENTITY_RUNTIME_SUSPENSION_DATABASE_URL_FILE",
        "identity_suspension_database_url",
    ),
    "DELETION": (
        "IDENTITY_RUNTIME_DELETION_DATABASE_URL_FILE",
        "identity_deletion_database_url",
    ),
    "OFFBOARDING": (
        "IDENTITY_RUNTIME_OFFBOARDING_DATABASE_URL_FILE",
        "identity_offboarding_database_url",
    ),
    "BRIEFS": (
        "IDENTITY_RUNTIME_BRIEFS_DATABASE_URL_FILE",
        "identity_briefs_database_url",
    ),
    "INVITATION": (
        "IDENTITY_RUNTIME_INVITATION_DATABASE_URL_FILE",
        "identity_invitation_database_url",
    ),
    "DEACTIVATION": (
        "IDENTITY_RUNTIME_DEACTIVATION_DATABASE_URL_FILE",
        "identity_deactivation_database_url",
    ),
    "DEACTIVATION_OPERATOR": (
        "IDENTITY_RUNTIME_DEACTIVATION_OPERATOR_DATABASE_URL_FILE",
        "identity_deactivation_operator_database_url",
    ),
    "RECOVERY": (
        "IDENTITY_RUNTIME_RECOVERY_DATABASE_URL_FILE",
        "identity_recovery_database_url",
    ),
}

_AUX_DATABASES = {
    "MAIL_SETTINGS": (
        "IDENTITY_RUNTIME_MAIL_SETTINGS_DATABASE_URL_FILE",
        "identity_mail_settings_database_url",
    ),
    "MAIL_V2": (
        "IDENTITY_RUNTIME_MAIL_V2_DATABASE_URL_FILE",
        "identity_mail_v2_database_url",
    ),
}

_DATABASES = {**_IDENTITY_DATABASES, **_AUX_DATABASES}
_SERVICES = (
    "identity-issuer",
    "identity-api",
    "identity-mail-v2",
)
_BOOTSTRAP_MINIO_ALIAS = "labeling-cold-minio"
_IDENTITY_OPTIONAL_SECRETS = {
    "IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE": "identity_admission_token",
    "IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE": "identity_actor_status_database_url",
}
_SERVICE_SECRET_MOUNTS = {
    "identity-issuer": {
        "identity_database_url",
        "identity_key_database_url",
        "identity_session_database_url",
        "identity_account_database_url",
        "identity_presence_database_url",
        "identity_group_database_url",
        "identity_directory_database_url",
        "identity_admin_password_database_url",
        "identity_platform_role_database_url",
        "identity_suspension_database_url",
        "identity_deletion_database_url",
        "identity_offboarding_database_url",
        "identity_briefs_database_url",
        "identity_invitation_database_url",
        "identity_deactivation_database_url",
        "identity_deactivation_operator_database_url",
        "identity_recovery_database_url",
        "identity_recovery_mail_key",
        "identity_login_redis_url",
        "identity_token",
        "identity_signing_secret",
    },
    "identity-mail-v2": {
        "identity_mail_v2_database_url",
        "identity_mail_settings_database_url",
        "identity_recovery_mail_key",
        "identity_mail_smtp_password",
    },
    "identity-api": set(),
}
_API_BINDING_NAMES = (
    "IDENTITY_AUTH_DATABASE_URL",
    "IDENTITY_KEY_DATABASE_URL",
    "IDENTITY_SESSION_DATABASE_URL",
    "IDENTITY_ACCOUNT_DATABASE_URL",
    "IDENTITY_GROUP_DATABASE_URL",
    "IDENTITY_DIRECTORY_DATABASE_URL",
    "IDENTITY_ADMIN_PASSWORD_DATABASE_URL",
    "IDENTITY_PLATFORM_ROLE_DATABASE_URL",
    "IDENTITY_SUSPENSION_DATABASE_URL",
    "IDENTITY_DELETION_DATABASE_URL",
    "IDENTITY_OFFBOARDING_DATABASE_URL",
    "IDENTITY_BRIEFS_DATABASE_URL",
    "IDENTITY_INVITATION_DATABASE_URL",
    "IDENTITY_DEACTIVATION_DATABASE_URL",
    "IDENTITY_DEACTIVATION_OPERATOR_DATABASE_URL",
    "IDENTITY_PRESENCE_DATABASE_URL",
    "IDENTITY_RECOVERY_DATABASE_URL",
)
_API_DATABASE_FILE_NAMES = tuple(f"{name}_FILE" for name in _API_BINDING_NAMES)
_READY_PROBE = (
    "import urllib.request; "
    "urllib.request.urlopen('http://127.0.0.1:8171/ready', timeout=2)"
)


def asyncpg_connection_url(value: str | URL) -> URL:
    """Normalize supported SQLAlchemy PostgreSQL URLs for asyncpg.connect."""
    try:
        url = make_url(value)
        if url.drivername not in {"postgresql", "postgresql+asyncpg"}:
            raise ValueError
        return url.set(drivername="postgresql")
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError(
            "Verifier role DSN is not an asyncpg PostgreSQL URL"
        ) from None


def verifier_database_url(
    value: str,
    reference_url: URL,
    *,
    expected_role: str,
    container_name: str,
) -> URL:
    """Bind one verifier role DSN to the same disposable database privately."""
    try:
        url = make_url(value)
        if (
            url.drivername not in {"postgresql", "postgresql+asyncpg"}
            or (url.host, url.port, url.database)
            != (reference_url.host, reference_url.port, reference_url.database)
            or url.username != expected_role
            or not url.password
            or url.database is None
            or not url.database.endswith("_test")
            or bool(url.query)
            or not container_name
        ):
            raise ValueError
        return asyncpg_connection_url(url).set(host=container_name, port=5432)
    except (ArgumentError, TypeError, ValueError):
        raise RuntimeError(
            "Verifier role DSN does not match the disposable database"
        ) from None


def _expected_secret_mount_names(
    service: str, *, jobs_publication: bool = False, embedded: bool = False
) -> set[str] | None:
    expected = _SERVICE_SECRET_MOUNTS.get(service)
    if expected is None:
        return None
    result = set(expected)
    if service == "identity-issuer":
        result.update(_IDENTITY_OPTIONAL_SECRETS.values())
    if service == "identity-api":
        if jobs_publication:
            result.add("jobs_publication_database_url")
        if embedded:
            result.add("identity_recovery_mail_key")
    return result


class IdentityModuleStack:
    """Run the existing Identity Compose services plus one isolated API image."""

    def __init__(
        self,
        test_engine,
        directory,
        *,
        secret: str,
        api_secret: str,
        control: str,
        mail_key: str,
        login_redis_url: str,
        smtp_port: int,
        embedded: bool = False,
        identity_actor_status_database_url: str | None = None,
        admission_token: str | None = None,
        jobs_publication: bool = False,
    ) -> None:
        self.test_engine = test_engine
        self._retention_guards = []
        self.directory = Path(directory).resolve()
        self._secret_directory = self.directory / "identity-module-secrets"
        self._compose_env_file = self._secret_directory / "compose.env"
        self._api_env_file = self._secret_directory / "api.env"
        self._override_file = self.directory / "identity-module-compose.override.yml"
        self._log_file = self.directory / "identity-module-compose.log"
        self._secret = secret
        self._api_secret = api_secret
        self._control = control
        self._mail_key = mail_key
        self._embedded = embedded
        self._jobs_publication = jobs_publication
        self._admission_token = admission_token
        if bool(identity_actor_status_database_url) != bool(admission_token):
            raise ValueError("Scoped Identity issuer fixture inputs are incomplete")
        if embedded and admission_token:
            raise ValueError("Embedded Identity cannot expose a scoped issuer")
        if len({secret, api_secret, control, mail_key}) != 4 or admission_token in {
            secret,
            api_secret,
            control,
            mail_key,
        }:
            raise ValueError(
                "Identity issuer, media, control and Fernet keys must differ"
            )
        _verify_postgres(test_engine.url)
        _verify_redis()
        self._postgres_name = self._required_environment(
            "AAP_MODULE_DATA_PG_CONTAINER_NAME"
        )
        self._postgres_container = self._required_environment(
            "AAP_MODULE_DATA_PG_CONTAINER_ID"
        )
        self._redis_name = self._required_environment(
            "AAP_MODULE_DATA_REDIS_CONTAINER_NAME"
        )
        self._login_redis_url = self._docker_url(login_redis_url)
        self._smtp_port = int(smtp_port)
        if not 1 <= self._smtp_port <= 65535:
            raise ValueError("SMTP port is outside the valid TCP range")

        self._identity_image = self._required_environment("AAP_IDENTITY_RUNTIME_IMAGE")
        self._api_image = self._required_environment("AAP_IDENTITY_API_RUNTIME_IMAGE")
        self._identity = self._required_environment("AAP_IDENTITY_RUNTIME_IDENTITY")
        self._api_identity = self._required_environment(
            "AAP_IDENTITY_API_RUNTIME_IDENTITY"
        )
        self.project_name = f"aap-identity-assembly-{uuid.uuid4().hex[:12]}"
        self._compose_environment = self._docker_environment()
        self._database_urls = {
            kind: make_url(_dsn(kind, test_engine)).set(
                host=self._postgres_name, port=5432
            )
            for kind in (_AUX_DATABASES if embedded else _DATABASES)
        }
        self._database_name = self._validate_database_name(
            str(test_engine.url.database or "")
        )
        self._jobs_publication_database_url = (
            verifier_database_url(
                self._required_environment("AAP_MODULE_DATA_JOBS_PUBLICATION_URL"),
                test_engine.url,
                expected_role="yoao_jobs_publication_client",
                container_name=self._postgres_name,
            )
            if jobs_publication
            else None
        )
        self._actor_status_database_url = (
            verifier_database_url(
                identity_actor_status_database_url,
                test_engine.url,
                expected_role="yoao_identity_actor_status_client",
                container_name=self._postgres_name,
            )
            if identity_actor_status_database_url is not None
            else None
        )
        api_dsn_var, api_role = (
            (
                "AAP_MODULE_DATA_IDENTITY_EMBEDDED_API_CONSUMER_URL",
                "identity_embedded_api_consumer",
            )
            if embedded
            else ("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL", "identity_api_consumer")
        )
        self._api_database_url = verifier_database_url(
            self._required_environment(api_dsn_var),
            test_engine.url,
            expected_role=api_role,
            container_name=self._postgres_name,
        ).set(drivername="postgresql+asyncpg")
        self._service_ids: dict[str, str] = {}
        self._inspection_evidence: dict[str, dict] = {}
        self._services = tuple(
            service
            for service in _SERVICES
            if not embedded or service != "identity-issuer"
        )
        self._api_origin = ""
        self._identity_origin = ""
        self._minio = self._bootstrap_minio()
        self._minio_attachment: tuple[str, str] | None = None
        self._verify_image(self._identity_image, self._identity)
        self._verify_image(self._api_image, self._api_identity)

    @property
    def api_origin(self) -> str:
        return self._api_origin

    @property
    def identity_origin(self) -> str:
        return self._identity_origin

    @property
    def service_ids(self) -> dict[str, str]:
        return dict(self._service_ids)

    @property
    def inspection_evidence(self) -> dict[str, dict]:
        return json.loads(json.dumps(self._inspection_evidence))

    def retain_while(self, guard) -> None:
        """Register an owner whose unproved work must keep the assembly intact."""
        self._retention_guards.append(guard)

    @property
    def retention_required(self) -> bool:
        return any(guard() for guard in self._retention_guards)

    def redact(self, value: str) -> str:
        values = [
            self._secret,
            self._api_secret,
            self._control,
            self._mail_key,
            self._admission_token or "",
            self._login_redis_url,
            make_url(self._login_redis_url).password or "",
            os.environ.get("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY", ""),
        ]
        urls = [self._api_database_url, *self._database_urls.values()]
        if self._actor_status_database_url is not None:
            urls.append(self._actor_status_database_url)
        if self._jobs_publication_database_url is not None:
            urls.append(self._jobs_publication_database_url)
        for url in urls:
            values.extend(
                [url.render_as_string(hide_password=False), url.password or ""]
            )
        for secret in sorted(set(values), key=len, reverse=True):
            if secret:
                value = value.replace(secret, "<redacted>")
        value = re.sub(
            r"[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}",
            "<redacted-jwt>",
            value,
        )
        return re.sub(r"ak_[A-Za-z0-9_-]+", "<redacted-key>", value)

    def capture_diagnostics(self) -> str:
        path = (
            ROOT / ".worktree" / f"identity-module-diagnostics-{self.project_name}.json"
        )
        compose_environment_ready = self._compose_env_file.is_file()
        override_ready = self._override_file.is_file()
        if not compose_environment_ready or not override_ready:
            records = {
                "state": "configuration_not_prepared",
                "compose_environment_ready": compose_environment_ready,
                "override_ready": override_ready,
            }
        else:
            records = {}
            containers = self._verify_project_ownership()
            for service, details in containers.items():
                result = subprocess.run(
                    ["docker", "logs", "--tail", "100", details["Id"]],
                    env=self._compose_environment,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                records[service] = {
                    "container_id": details.get("Id"),
                    "image_id": details.get("Image"),
                    "networks": {
                        name: {
                            "network_id": attachment.get("NetworkID"),
                            "endpoint_id": attachment.get("EndpointID"),
                            "address": attachment.get("IPAddress"),
                        }
                        for name, attachment in (
                            (details.get("NetworkSettings") or {}).get("Networks") or {}
                        ).items()
                    },
                    "mount_sources": [
                        {
                            "source": mount.get("Source"),
                            "destination": mount.get("Destination"),
                        }
                        for mount in details.get("Mounts") or []
                    ],
                    "state": details.get("State"),
                    "logs": self.redact(result.stdout + result.stderr),
                }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(json.dumps(records, indent=2) + "\n")
        path.chmod(0o600)
        return str(path)

    async def start(self, database_name: str | None = None) -> None:
        if database_name is not None:
            self._database_name = self._validate_database_name(database_name)
            self._api_database_url = self._api_database_url.set(
                database=self._database_name
            )
        self._prepare_files()
        self._verify_project_ownership()
        self._compose("create", timeout=180)
        self._connect_dependencies()
        self._compose("up", "--detach", timeout=180)
        self._refresh_origins()
        await self._wait_ready()
        self._refresh_inspection_evidence()

    async def stop(self) -> None:
        if self.retention_required:
            raise RuntimeError(
                "Identity assembly retained: owner retirement is unproved"
            )
        if not self._compose_env_file.is_file() or not self._override_file.is_file():
            return
        containers = self._verify_project_ownership()
        if containers:
            self._compose("stop", timeout=90)
        self._disconnect_dependencies()
        if containers:
            self._compose("down", timeout=90)
        self._service_ids = {}

    def _bootstrap_minio(self) -> dict:
        from app.config import settings
        from tests.test_asset_storage_cleanup_runtime import (
            _shared_bootstrap_minio,
            _verified_resource_manifest,
        )

        resources = _verified_resource_manifest(self.test_engine, self.test_engine.url)
        manifest_path = Path(self._required_environment("TMPDIR")).resolve().parents[
            2
        ] / (".worktree/test/resources.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        minio = manifest.get("minio") or {}
        if (
            manifest.get("resources") != resources
            or minio.get("endpoint") != settings.minio_endpoint
            or minio.get("ssl") is not settings.minio_use_ssl
        ):
            raise RuntimeError("Bootstrap MinIO differs from the owned manifest")
        endpoint = urlsplit(f"http://{settings.minio_endpoint}")
        container_id = self._required_environment("AAP_MODULE_DATA_MINIO_CONTAINER_ID")
        shared = _shared_bootstrap_minio(settings, container_id=container_id)
        if (
            endpoint.port is None
            or re.fullmatch(r"[0-9a-f]{64}", container_id) is None
            or (
                shared is None
                and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            )
        ):
            raise RuntimeError("Bootstrap MinIO selector is invalid")
        details = shared if shared is not None else self._inspect(container_id)
        image_id = details.get("Image")
        bindings = (details.get("NetworkSettings", {}).get("Ports") or {}).get(
            "9000/tcp"
        ) or []
        if not (
            details.get("Id") == container_id
            and details.get("Name")
            and isinstance(image_id, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            and details.get("State", {}).get("Running") is True
            and (details.get("Config", {}).get("Labels") or {}).get(
                "com.docker.compose.service"
            )
            == "minio"
            and len(bindings) == 1
            and (
                shared is not None or bindings[0].get("HostIp") in {"127.0.0.1", "::1"}
            )
            and bindings[0].get("HostPort") == str(endpoint.port)
        ):
            raise RuntimeError("Bootstrap MinIO identity or loopback binding differs")
        return {
            "id": container_id,
            "name": details["Name"],
            "image_id": image_id,
            "host_port": endpoint.port,
            "host_binding": dict(bindings[0]),
            "service_port": 9000,
            "shared": shared is not None,
            "endpoint": settings.minio_endpoint,
        }

    def _verify_bootstrap_minio(self, details):
        if not self._minio["shared"]:
            from tests.test_labeling_module_api_cold import _verify_minio_identity

            _verify_minio_identity(details, self._minio)
            return
        from app.config import settings
        from tests.test_asset_storage_cleanup_runtime import _shared_bootstrap_minio

        shared = _shared_bootstrap_minio(settings, container_id=self._minio["id"])
        if (
            shared is None
            or any(
                details.get(name) != shared.get(name)
                for name in ("Id", "Name", "Image")
            )
            or any(
                details.get(name) != self._minio[key]
                for name, key in (("Id", "id"), ("Name", "name"), ("Image", "image_id"))
            )
        ):
            raise RuntimeError("Shared bootstrap MinIO identity changed")

    def _dependency_network(self):
        name = self.project_name + "_default"
        ids = self._docker(
            ["network", "ls", "--filter", f"name=^{name}$", "--quiet"],
            operation="owned network lookup",
        )
        if not ids:
            return None
        if len(ids.splitlines()) != 1:
            raise RuntimeError("Owned Compose network identity is ambiguous")
        record = json.loads(
            self._docker(
                ["network", "inspect", ids], operation="owned network inspection"
            )
        )[0]
        if (
            record["Name"] != name
            or record.get("Labels", {}).get("com.docker.compose.project")
            != self.project_name
        ):
            raise RuntimeError("Network is not owned by this Compose project")
        return record

    def _connect_dependencies(self):
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        network = self._dependency_network()
        if network is None:
            raise RuntimeError("Compose did not create its owned network")
        network_name, network_id = network["Name"], network["Id"]
        for container in (self._postgres_container, self._redis_name):
            details = self._inspect(container)
            if network_name not in details["NetworkSettings"]["Networks"]:
                self._docker(
                    [
                        "network",
                        "connect",
                        "--gw-priority",
                        "-1",
                        network_id,
                        container,
                    ],
                    operation="owned dependency attachment",
                )
        details = self._inspect(self._minio["id"])
        self._verify_bootstrap_minio(details)
        if network_name in details["NetworkSettings"]["Networks"]:
            raise RuntimeError("Bootstrap MinIO already has an unowned attachment")
        self._docker(
            [
                "network",
                "connect",
                "--gw-priority",
                "-1",
                "--alias",
                _BOOTSTRAP_MINIO_ALIAS,
                network_id,
                self._minio["id"],
            ],
            operation="bootstrap storage attachment",
        )
        details = self._inspect(self._minio["id"])
        self._verify_bootstrap_minio(details)
        endpoint = details["NetworkSettings"]["Networks"].get(network_name) or {}
        if (
            endpoint.get("NetworkID") != network_id
            or not endpoint.get("EndpointID")
            or _BOOTSTRAP_MINIO_ALIAS not in (endpoint.get("Aliases") or [])
        ):
            raise RuntimeError("Bootstrap MinIO attachment was not verified")
        self._minio_attachment = (network_id, endpoint["EndpointID"])

    def _disconnect_dependencies(self):
        network = self._dependency_network()
        if network is None:
            return
        network_name, network_id = network["Name"], network["Id"]
        details = self._inspect(self._minio["id"])
        self._verify_bootstrap_minio(details)
        endpoint = details["NetworkSettings"]["Networks"].get(network_name)
        if endpoint:
            if (
                self._minio_attachment != (network_id, endpoint.get("EndpointID"))
                or endpoint.get("NetworkID") != network_id
                or not endpoint.get("EndpointID")
                or _BOOTSTRAP_MINIO_ALIAS not in (endpoint.get("Aliases") or [])
            ):
                raise RuntimeError("Refusing to detach an uncertain MinIO attachment")
            self._docker(
                ["network", "disconnect", network_id, self._minio["id"]],
                operation="bootstrap storage detachment",
            )
            detached = self._inspect(self._minio["id"])
            self._verify_bootstrap_minio(detached)
            if network_name in detached["NetworkSettings"]["Networks"]:
                raise RuntimeError("Bootstrap MinIO remained attached")
            self._minio_attachment = None
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        for container in (self._postgres_container, self._redis_name):
            details = self._inspect(container)
            if network_name in details["NetworkSettings"]["Networks"]:
                self._docker(
                    ["network", "disconnect", network_id, container],
                    operation="owned dependency detachment",
                )

    async def restart(self, service: str) -> None:
        self._validate_service(service)
        self._prepare_files()
        self._verify_project_ownership()
        self._compose("restart", service, timeout=90)
        self._refresh_origins()
        await self._wait_service_ready(service)
        self._refresh_inspection_evidence()

    async def start_service(self, service: str) -> None:
        self._validate_service(service)
        self._prepare_files()
        self._verify_project_ownership()
        self._compose("start", service, timeout=90)
        self._refresh_origins()
        await self._wait_service_ready(service)
        self._refresh_inspection_evidence()

    async def stop_service(self, service: str) -> None:
        self._validate_service(service)
        self._prepare_files()
        self._verify_project_ownership()
        self._compose("stop", service, timeout=90)
        self._refresh_inspection_evidence()

    async def kill_service(self, service: str) -> None:
        """Terminate one owned service without Compose's graceful stop window."""
        self._validate_service(service)
        self._prepare_files()
        self._verify_project_ownership()
        self._compose("kill", service, timeout=30)
        self._refresh_inspection_evidence()

    async def switch_database(self, database_name: str) -> None:
        if self.retention_required:
            raise RuntimeError(
                "Identity assembly retained: owner retirement is unproved"
            )
        database_name = self._validate_database_name(database_name)
        self._prepare_files()
        containers = self._verify_project_ownership()
        if containers:
            self._compose("stop", timeout=90)
        self._database_name = database_name
        self._api_database_url = self._api_database_url.set(database=database_name)
        self._prepare_files()
        if not containers:
            await self.start()
            return
        self._compose("up", "--detach", "--force-recreate", timeout=180)
        self._refresh_origins()
        await self._wait_ready()
        self._refresh_inspection_evidence()

    def _prepare_files(self) -> None:
        self._secret_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._secret_directory.chmod(0o700)
        if self._secret_directory.is_symlink():
            raise RuntimeError("Identity secret directory must not be a symlink")

        compose_values = {
            "AAP_IDENTITY_RUNTIME_IMAGE": self._identity_image,
            "AAP_IDENTITY_API_RUNTIME_IMAGE": self._api_image,
            "AAP_IDENTITY_RUNTIME_IDENTITY": self._identity,
            "AAP_IDENTITY_API_RUNTIME_IDENTITY": self._api_identity,
            "IDENTITY_MODULE_STACK_ID": self.project_name,
            "IDENTITY_API_ENV_FILE": str(self._api_env_file),
            "IDENTITY_RUNTIME_ALLOW_OPEN_REGISTRATION": "true",
            "IDENTITY_RUNTIME_EXPIRE_MINUTES": "1440",
            "IDENTITY_MAIL_SMTP_HOST": "host.docker.internal",
            "IDENTITY_MAIL_SMTP_PORT": str(self._smtp_port),
            "IDENTITY_MAIL_SMTP_FROM": "noreply@example.test",
        }

        def secret(selector, name, value):
            self._write_secret(name, value)
            compose_values[selector] = str(self._secret_directory / name)

        for kind, (selector, name) in _DATABASES.items():
            if kind in self._database_urls:
                url = self._database_urls[kind].set(database=self._database_name)
                secret(selector, name, url.render_as_string(hide_password=False))
        secret(
            "IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE",
            "identity_recovery_mail_key",
            self._mail_key,
        )
        if not self._embedded:
            secret(
                "IDENTITY_RUNTIME_LOGIN_REDIS_URL_FILE",
                "identity_login_redis_url",
                self._login_redis_url,
            )
            secret("IDENTITY_RUNTIME_TOKEN_FILE", "identity_token", self._control)
            secret(
                "IDENTITY_RUNTIME_SIGNING_SECRET_FILE",
                "identity_signing_secret",
                self._secret,
            )
        if self._actor_status_database_url is not None:
            url = self._actor_status_database_url.set(database=self._database_name)
            secret(
                "IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE",
                "identity_actor_status_database_url",
                url.render_as_string(hide_password=False),
            )
            secret(
                "IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE",
                "identity_admission_token",
                self._admission_token,
            )
        if self._jobs_publication_database_url is not None:
            url = self._jobs_publication_database_url.set(database=self._database_name)
            secret(
                "JOBS_PUBLICATION_DATABASE_URL_SECRET_FILE",
                "jobs_publication_database_url",
                url.render_as_string(hide_password=False),
            )
        self._write_environment_file(self._api_env_file, self._api_environment())
        self._write_environment_file(self._compose_env_file, compose_values)
        self._write_override_file()

    def _api_environment(self) -> dict[str, str]:
        database_url = self._api_database_url.render_as_string(hide_password=False)
        redis_url = self._docker_url(self._required_environment("REDIS_URL"))
        celery_url = self._docker_url(
            os.environ.get("CELERY_BROKER_URL")
            or self._required_environment("REDIS_URL")
        )
        values = {
            "SECRET_KEY": self._api_secret,
            "DATABASE_URL": database_url,
            "REDIS_URL": redis_url,
            "CELERY_BROKER_URL": celery_url,
            "IDENTITY_URL": "" if self._embedded else "http://identity-issuer:8170",
            "IDENTITY_TOKEN": "" if self._embedded else self._control,
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "AUDIT_ASYNC": "true",
            "MIGRATION_DATABASE_URL": "",
            "ALLOW_OPEN_REGISTRATION": "false",
            "TURNSTILE_ENABLED": "false",
            "SENTRY_DSN": "",
            "DEBUG": "false",
            "OPENBLAS_NUM_THREADS": "1",
            "MINIO_ENDPOINT": self._docker_endpoint(
                self._required_environment("MINIO_ENDPOINT")
            ),
            "MINIO_ACCESS_KEY": self._required_environment(
                "AAP_MODULE_DATA_API_BOOTSTRAP_S3_ACCESS_KEY"
            ),
            "MINIO_SECRET_KEY": self._required_environment(
                "AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY"
            ),
        }
        if self._jobs_publication:
            values["JOBS_PUBLICATION_DATABASE_URL_FILE"] = (
                "/run/secrets/jobs_publication_database_url"
            )
        for env_name, default in (
            ("MINIO_BUCKET", "annotations"),
            ("MINIO_DATASETS_BUCKET", "datasets"),
            ("MINIO_BUG_REPORTS_BUCKET", "bug-reports"),
            ("MINIO_MEDIA_CACHE_BUCKET", "media-cache"),
            ("MINIO_AUDIT_ARCHIVE_BUCKET", "audit-archive"),
            ("MINIO_IMPORT_BUCKET", "import"),
            ("MINIO_EXPORT_BUCKET", "export"),
            ("MINIO_AVATARS_BUCKET", "avatars"),
            ("MINIO_TRAINING_BUCKET", "training"),
            ("MINIO_INPUTS_BUCKET", "accepted-inputs"),
            ("MINIO_USE_SSL", "false"),
        ):
            values[env_name] = os.environ.get(env_name, default)
        public_url = os.environ.get("MINIO_PUBLIC_URL", "")
        if self._minio["shared"]:
            shared_url = (
                f"{'https' if values['MINIO_USE_SSL'].lower() == 'true' else 'http'}://"
                f"{self._minio['endpoint']}"
            )
            if public_url not in {"", "/minio", shared_url, shared_url + "/"}:
                raise RuntimeError("Public MinIO URL differs from the shared bootstrap")
            values["MINIO_PUBLIC_URL"] = shared_url
        else:
            values["MINIO_PUBLIC_URL"] = (
                self._docker_http_url(public_url)
                if public_url.startswith("http")
                else public_url
                or (
                    f"{'https' if values['MINIO_USE_SSL'].lower() == 'true' else 'http'}://"
                    f"{self._required_environment('MINIO_ENDPOINT')}"
                )
            )
        for name in _API_BINDING_NAMES + _API_DATABASE_FILE_NAMES:
            values[name] = ""
        if self._embedded:
            values["IDENTITY_EMBEDDED_SIGNING_SECRET"] = self._secret
            values["IDENTITY_RECOVERY_MAIL_KEY_FILE"] = (
                "/run/secrets/identity_recovery_mail_key"
            )
        return values

    def _write_override_file(self) -> None:
        api_env_file = json.dumps(str(self._api_env_file))
        api_secret_names = []
        if self._embedded:
            api_secret_names.append("identity_recovery_mail_key")
        if self._jobs_publication:
            api_secret_names.append("jobs_publication_database_url")
        api_secrets = (
            "    secrets:\n" + "".join(f"      - {name}\n" for name in api_secret_names)
            if api_secret_names
            else ""
        )
        secrets_block = ""
        if self._jobs_publication:
            secrets_block = """secrets:
  jobs_publication_database_url:
    file: ${JOBS_PUBLICATION_DATABASE_URL_SECRET_FILE:?}
"""
        issuer_dependency = (
            ""
            if self._embedded
            else """    depends_on:
      identity-issuer:
        condition: service_healthy
"""
        )
        self._override_file.write_text(
            f"""services:
  identity-issuer:
    image: ${{AAP_IDENTITY_RUNTIME_IMAGE:?}}
    build: !reset null
    labels: &identity-module-owner-labels
      org.aap.acceptance: module-data-boundary
      org.aap.acceptance-id: ${{IDENTITY_MODULE_STACK_ID:?}}
    ports: !override
      - "127.0.0.1::8170"
  identity-mail-v2:
    image: ${{AAP_IDENTITY_RUNTIME_IMAGE:?}}
    build: !reset null
    restart: "no"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    labels: *identity-module-owner-labels
  identity-api:
    image: ${{AAP_IDENTITY_API_RUNTIME_IMAGE:?}}
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "3"]
    user: "10003:10003"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    env_file:
      - {api_env_file}
{issuer_dependency.rstrip()}
    ports: !override
      - "127.0.0.1::8000"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 64
    mem_limit: 512m
    cpus: 1
    tmpfs: ["/tmp:rw,noexec,nosuid,size=16m"]
    labels: *identity-module-owner-labels
{api_secrets.rstrip()}
{secrets_block.rstrip()}
""",
            encoding="utf-8",
        )
        self._override_file.chmod(0o600)

    def _write_secret(self, name: str, value: str) -> None:
        if not isinstance(value, str) or "\x00" in value or "\n" in value:
            raise ValueError("Identity secret values must be single-line text")
        path = self._secret_directory / name
        if path.is_symlink():
            raise RuntimeError("Identity secret file must not be a symlink")
        if path.exists():
            if path.read_text(encoding="utf-8") == value:
                path.chmod(0o444)
                return
            path.chmod(0o600)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o444)

    @staticmethod
    def _write_environment_file(path: Path, values: dict[str, str]) -> None:
        rows = []
        for name, raw_value in sorted(values.items()):
            value = str(raw_value)
            if "\x00" in value or "\n" in value:
                raise ValueError("Compose environment values must be single-line text")
            rows.append(f"{name}={json.dumps(value, ensure_ascii=False)}")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        path.chmod(0o600)

    def _write_log(self, message: str) -> None:
        with self._log_file.open("a", encoding="utf-8") as log:
            log.write(message + "\n")
        self._log_file.chmod(0o600)

    def _docker(
        self, arguments: list[str], *, operation: str, timeout: int = 90
    ) -> str:
        try:
            result = subprocess.run(
                ["docker", *arguments],
                env=self._compose_environment,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self._write_log(f"{operation}: timed out")
            raise RuntimeError(
                f"Owned Identity {operation} timed out; see {self._log_file}"
            ) from None
        if result.returncode != 0:
            self._write_log(f"{operation}: Docker exited {result.returncode}")
            raise RuntimeError(
                f"Owned Identity {operation} failed; see {self._log_file}"
            )
        return result.stdout.strip()

    def _compose_arguments(self) -> list[str]:
        arguments = [
            "compose",
            "--project-name",
            self.project_name,
            "--project-directory",
            str(self.directory),
            "--env-file",
            str(self._compose_env_file),
            "-f",
            str(ROOT / "docker-compose.identity.yml"),
        ]
        arguments.extend(["-f", str(self._override_file)])
        if not self._embedded:
            arguments.extend(["--profile", "identity-issuer"])
        return arguments

    def _compose(self, *arguments: str, timeout: int = 90) -> str:
        return self._docker(
            self._compose_arguments() + list(arguments),
            operation="Compose operation",
            timeout=timeout,
        )

    def _inspect(self, container_id: str) -> dict:
        output = self._docker(
            ["inspect", container_id], operation="container inspection", timeout=20
        )
        try:
            records = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError(
                "Docker returned an invalid owned container record"
            ) from None
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Owned container identity was not unique")
        return records[0]

    def _verify_image(
        self,
        image: str,
        identity: str,
        *,
        acceptance: str = "identity-admission",
        expected_user: str | None = None,
        expected_source_revision: str | None = None,
    ) -> None:
        output = self._docker(
            ["image", "inspect", image], operation="image inspection", timeout=30
        )
        try:
            records = json.loads(output)
        except json.JSONDecodeError:
            raise RuntimeError(
                "Docker returned an invalid owned image record"
            ) from None
        if not isinstance(records, list) or len(records) != 1:
            raise RuntimeError("Owned image identity was not unique")
        labels = (records[0].get("Config") or {}).get("Labels") or {}
        if (
            labels.get("org.aap.acceptance") != acceptance
            or labels.get("org.aap.acceptance-id") != identity
            or (
                expected_source_revision is not None
                and labels.get("org.aap.source-revision") != expected_source_revision
            )
            or (expected_source_revision is not None and records[0].get("Id") != image)
            or (
                expected_user is not None
                and (records[0].get("Config") or {}).get("User") != expected_user
            )
        ):
            raise RuntimeError("Runtime image labels do not match this verifier")

    def _verify_project_ownership(self) -> dict[str, dict]:
        raw_ids = self._compose("ps", "--all", "--quiet", timeout=20)
        container_ids = [value for value in raw_ids.splitlines() if value]
        containers: dict[str, dict] = {}
        for container_id in container_ids:
            details = self._inspect(container_id)
            labels = (details.get("Config") or {}).get("Labels") or {}
            if (
                labels.get("com.docker.compose.project") != self.project_name
                or labels.get("org.aap.acceptance") != "module-data-boundary"
                or labels.get("org.aap.acceptance-id") != self.project_name
            ):
                raise RuntimeError("Compose project contains a foreign container")
            service = labels.get("com.docker.compose.service")
            if not service or service in containers:
                raise RuntimeError("Compose project service identity is ambiguous")
            containers[service] = details
        self._service_ids = {
            service: details["Id"]
            for service, details in containers.items()
            if details.get("Id")
        }
        return containers

    def _refresh_origins(self) -> None:
        if not self._embedded:
            self._identity_origin = self._published_origin("identity-issuer", 8170)
        self._api_origin = self._published_origin("identity-api", 8000)

    def _published_origin(self, service: str, target_port: int) -> str:
        output = self._compose("port", service, str(target_port), timeout=20)
        values = [line.strip() for line in output.splitlines() if line.strip()]
        if len(values) != 1:
            raise RuntimeError("Owned loopback port mapping is not unique")
        parsed = urlsplit("//" + values[0])
        if parsed.hostname != "127.0.0.1" or parsed.port is None:
            raise RuntimeError("Owned service is not published on loopback")
        return f"http://127.0.0.1:{parsed.port}"

    async def _wait_http(self, url: str, operation: str, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                try:
                    if (await client.get(url)).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
        self._write_log(f"{operation}: readiness timed out")
        raise RuntimeError(
            f"Owned Identity {operation} did not become ready; see {self._log_file}"
        )

    def _mail_ready_probe(self, service: str) -> bool:
        try:
            result = subprocess.run(
                [
                    "docker",
                    *self._compose_arguments(),
                    "exec",
                    "-T",
                    service,
                    "python",
                    "-c",
                    _READY_PROBE,
                ],
                env=self._compose_environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False
        return result.returncode == 0

    async def _wait_mail_ready(self, service: str, timeout: float = 45) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await asyncio.to_thread(self._mail_ready_probe, service):
                return
            await asyncio.sleep(0.2)
        self._write_log(f"{service}: readiness timed out")
        raise RuntimeError(
            f"Owned Identity {service} did not become ready; see {self._log_file}"
        )

    async def _wait_service_ready(self, service: str) -> None:
        if service == "identity-issuer":
            await self._wait_http(self._identity_origin + "/ready", service)
        elif service == "identity-api":
            await self._wait_http(self._api_origin + "/health/db", service)
        elif service == "identity-mail-v2":
            await self._wait_mail_ready(service)

    async def _wait_ready(self) -> None:
        for service in self._services:
            await self._wait_service_ready(service)

    def _refresh_inspection_evidence(self) -> None:
        containers = self._verify_project_ownership()
        evidence: dict[str, dict] = {}
        for service, details in containers.items():
            config = details.get("Config") or {}
            host = details.get("HostConfig") or {}
            state = details.get("State") or {}
            mounts = details.get("Mounts") or []
            environment = config.get("Env") or []
            env_values = dict(item.split("=", 1) for item in environment if "=" in item)
            secret_targets = {
                mount.get("Destination")
                for mount in mounts
                if str(mount.get("Destination", "")).startswith("/run/secrets/")
            }
            self._assert_service_runtime(
                service, config, host, state, mounts, env_values
            )
            evidence[service] = {
                "image_id": details.get("Image"),
                "user": config.get("User"),
                "read_only": host.get("ReadonlyRootfs"),
                "secret_mount_targets": sorted(secret_targets),
                "mount_targets": sorted(
                    mount.get("Destination", "") for mount in mounts
                ),
                "environment_names": sorted(env_values),
                "limits": {
                    "memory_bytes": host.get("Memory"),
                    "nano_cpus": host.get("NanoCpus"),
                    "pids_limit": host.get("PidsLimit"),
                    "cap_drop": sorted(host.get("CapDrop") or []),
                    "security_options": sorted(host.get("SecurityOpt") or []),
                },
                "running": state.get("Running") is True,
                "oom_killed": state.get("OOMKilled") is True,
                "health": (state.get("Health") or {}).get("Status"),
            }
        self._inspection_evidence = evidence

    def _assert_service_runtime(
        self,
        service: str,
        config: dict,
        host: dict,
        state: dict,
        mounts: list,
        env: dict,
    ) -> None:
        if config.get("User") != "10003:10003":
            raise RuntimeError(f"{service} does not run as UID/GID 10003:10003")
        if host.get("ReadonlyRootfs") is not True:
            raise RuntimeError(f"{service} does not have a read-only root filesystem")
        if "ALL" not in (host.get("CapDrop") or []):
            raise RuntimeError(f"{service} does not drop all Linux capabilities")
        if "no-new-privileges:true" not in (host.get("SecurityOpt") or []):
            raise RuntimeError(f"{service} does not disable privilege escalation")
        expected_memory = (
            512 * 1024 * 1024 if service == "identity-api" else 256 * 1024 * 1024
        )
        if (
            host.get("Memory") != expected_memory
            or host.get("NanoCpus") != 1_000_000_000
            or host.get("PidsLimit") != 64
        ):
            raise RuntimeError(
                f"{service} resource limits differ from the acceptance budget"
            )
        if state.get("OOMKilled") is True:
            raise RuntimeError(f"{service} was OOM-killed")

        expected = _expected_secret_mount_names(
            service, jobs_publication=self._jobs_publication, embedded=self._embedded
        )
        targets = {
            mount.get("Destination")
            for mount in mounts
            if str(mount.get("Destination", "")).startswith("/run/secrets/")
        }
        if expected is None or targets != {f"/run/secrets/{name}" for name in expected}:
            raise RuntimeError(f"{service} secret mounts cross service boundaries")
        for mount in mounts:
            source = mount.get("Source")
            if source and Path(source).resolve().is_relative_to(ROOT):
                raise RuntimeError(f"{service} mounts the application source tree")
            if mount.get("Destination") in targets and mount.get("RW") is not False:
                raise RuntimeError(f"{service} secret mounts must be read-only")
        if service == "identity-issuer":
            for name, target in (
                ("IDENTITY_ADMISSION_TOKEN_FILE", "identity_admission_token"),
                (
                    "IDENTITY_ACTOR_STATUS_DATABASE_URL_FILE",
                    "identity_actor_status_database_url",
                ),
            ):
                selected = "/run/secrets/" + target if self._admission_token else ""
                if env.get(name, "") != selected:
                    raise RuntimeError("Identity scoped issuer credentials differ")
        elif service == "identity-api":
            if env.get("OPENBLAS_NUM_THREADS") != "1":
                raise RuntimeError(
                    "API native thread budget differs from its CPU limit"
                )
            if any(
                env.get(name, "")
                for name in _API_BINDING_NAMES + _API_DATABASE_FILE_NAMES
            ):
                raise RuntimeError(
                    "API runtime contains a direct Identity database binding"
                )
            if env.get("SECRET_KEY") != self._api_secret:
                raise RuntimeError("API runtime does not have its media URL secret")
            if self._embedded:
                if (
                    env.get("IDENTITY_URL")
                    or env.get("IDENTITY_TOKEN")
                    or env.get("IDENTITY_EMBEDDED_SIGNING_SECRET") != self._secret
                ):
                    raise RuntimeError("API is not using its embedded Identity signer")
            elif (
                "IDENTITY_SIGNING_SECRET" in env
                or "IDENTITY_SIGNING_SECRET_FILE" in env
                or self._secret in env.values()
                or env.get("IDENTITY_TOKEN") != self._control
                or env.get("IDENTITY_URL") != "http://identity-issuer:8170"
            ):
                raise RuntimeError(
                    "API runtime does not isolate the opt-in issuer signer"
                )
            expected_publication = (
                "/run/secrets/jobs_publication_database_url"
                if self._jobs_publication
                else ""
            )
            if (
                env.get("JOBS_PUBLICATION_DATABASE_URL")
                or env.get("JOBS_PUBLICATION_DATABASE_URL_FILE", "")
                != expected_publication
                or any(
                    value
                    for key, value in env.items()
                    if key.startswith(("JOBS_QUEUE_", "JOBS_DISPATCH_"))
                )
            ):
                raise RuntimeError(
                    "API Jobs publication binding crosses credential boundaries"
                )
        elif service == "identity-mail-v2":
            if any(bindings for bindings in (host.get("PortBindings") or {}).values()):
                raise RuntimeError("Identity mail must not publish a host port")
            if any(
                (
                    name.startswith("IDENTITY_")
                    and name not in {"IDENTITY_HOST", "IDENTITY_PORT"}
                    and not name.startswith("IDENTITY_MAIL_V2_")
                )
                or name
                in {"DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "SECRET_KEY"}
                for name in env
            ) or any(
                value in env.values()
                for value in (self._secret, self._api_secret, self._control)
            ):
                raise RuntimeError(
                    "Identity mail received unrelated runtime credentials"
                )

    def _validate_service(self, service: str) -> None:
        if service not in self._services:
            raise ValueError("Service is outside this owned Identity Compose project")

    @staticmethod
    def _validate_database_name(database_name: str) -> str:
        if (
            not isinstance(database_name, str)
            or len(database_name) > 63
            or re.fullmatch(r"[A-Za-z0-9_]+", database_name) is None
            or not database_name.endswith("_test")
        ):
            raise ValueError(
                "Restored database must be a safe disposable _test identifier"
            )
        return database_name

    @staticmethod
    def _required_environment(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"Verifier input {name} is missing")
        return value

    @staticmethod
    def _docker_environment() -> dict[str, str]:
        names = (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "XDG_RUNTIME_DIR",
        )
        return {name: os.environ[name] for name in names if os.environ.get(name)}

    def _docker_url(self, value: str) -> str:
        try:
            url = make_url(value)
            if not url.host:
                raise ValueError
            return url.set(host=self._redis_name, port=6379).render_as_string(
                hide_password=False
            )
        except (TypeError, ValueError):
            raise ValueError("Verifier supplied an invalid service URL") from None

    def _docker_endpoint(self, value: str) -> str:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Verifier supplied an invalid object storage endpoint")
        _ = parsed.port
        if self._minio["shared"]:
            if value != self._minio["endpoint"]:
                raise ValueError("Shared bootstrap MinIO endpoint changed")
            self._verify_bootstrap_minio(self._inspect(self._minio["id"]))
            return value
        return f"{_BOOTSTRAP_MINIO_ALIAS}:9000"

    @classmethod
    def _docker_http_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            not parsed.scheme
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Verifier supplied an invalid public object storage URL")
        port = parsed.port
        endpoint = (
            f"host.docker.internal:{port}"
            if port is not None
            else "host.docker.internal"
        )
        return f"{parsed.scheme}://{endpoint}{parsed.path}"
