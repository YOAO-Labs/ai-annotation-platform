"""Owned Compose assembly helpers for the complete Identity verifier."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import httpx
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

from tests.test_identity_admission_live import ROOT, _dsn
from tests.test_asset_storage_cleanup_runtime import _verify_postgres
from tests.test_identity_sessions_live import _verify_redis


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
    "MAIL": ("IDENTITY_RUNTIME_MAIL_DATABASE_URL_FILE", "identity_mail_database_url"),
    "MAIL_SETTINGS": (
        "IDENTITY_RUNTIME_MAIL_SETTINGS_DATABASE_URL_FILE",
        "identity_mail_settings_database_url",
    ),
    "MAIL_V2": (
        "IDENTITY_RUNTIME_MAIL_V2_DATABASE_URL_FILE",
        "identity_mail_v2_database_url",
    ),
    "NOTIFICATION_PUBLISH": (
        "IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_DATABASE_URL_FILE",
        "notification_publish_database_url",
    ),
}

_DATABASES = {**_IDENTITY_DATABASES, **_AUX_DATABASES}
_SERVICES = (
    "identity-admission",
    "identity-api",
    "identity-mail",
    "identity-mail-v2",
    "identity-notification-publisher",
)
_NOTIFICATIONS_SOCKET_SERVICE = "notifications-socket"
_NOTIFICATIONS_SOCKET_IMAGE_ACCEPTANCE = "notifications-socket-runtime"
_NOTIFICATIONS_PUBLISHER_IMAGE_ACCEPTANCE = "notifications-delivery-runtime"
_JOBS_QUEUE_SERVICE = "jobs-queue"
_JOBS_QUEUE_IMAGE_ACCEPTANCE = "jobs-queue-runtime"
_JOBS_QUEUE_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_JOBS_QUEUE_ACCEPTANCE_ID = re.compile(r"[0-9a-f]{24}\Z")
_JOBS_QUEUE_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_IDENTITY_OPTIONAL_SECRETS = {
    "IDENTITY_RUNTIME_ADMISSION_TOKEN_FILE": "identity_admission_token",
    "IDENTITY_RUNTIME_ACTOR_STATUS_DATABASE_URL_FILE": "identity_actor_status_database_url",
}
_SERVICE_SECRET_MOUNTS = {
    "identity-admission": {
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
    "identity-mail": {
        "identity_mail_database_url",
        "identity_mail_settings_database_url",
        "identity_recovery_mail_key",
    },
    "identity-mail-v2": {
        "identity_mail_v2_database_url",
        "identity_mail_settings_database_url",
        "identity_recovery_mail_key",
    },
    "identity-notification-publisher": {
        "notification_publish_database_url",
        "notification_publish_redis_url",
    },
    "identity-api": set(),
    _NOTIFICATIONS_SOCKET_SERVICE: {
        "notification_socket_database_url",
        "notification_socket_redis_url",
        "notification_socket_identity_token",
    },
    _JOBS_QUEUE_SERVICE: {"jobs_queue_broker_url", "jobs_queue_token"},
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
    "IDENTITY_MAIL_DATABASE_URL",
    "IDENTITY_MAIL_SETTINGS_DATABASE_URL",
    "IDENTITY_MAIL_V2_DATABASE_URL",
    "NOTIFICATION_PUBLISH_DATABASE_URL",
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


def _identity_optional_secret_selectors(
    secret_directory: Path, notifications_socket_enabled: bool
) -> dict[str, str]:
    return {
        environment_name: (
            str(secret_directory / secret_name) if notifications_socket_enabled else ""
        )
        for environment_name, secret_name in _IDENTITY_OPTIONAL_SECRETS.items()
    }


def _expected_secret_mount_names(
    service: str,
    notifications_socket_enabled: bool,
    notification_publication_enabled: bool = False,
    jobs_queue_enabled: bool = False,
) -> set[str] | None:
    expected = _SERVICE_SECRET_MOUNTS.get(service)
    if (
        expected is None
        or (
            service == _NOTIFICATIONS_SOCKET_SERVICE
            and not notifications_socket_enabled
        )
        or (service == _JOBS_QUEUE_SERVICE and not jobs_queue_enabled)
    ):
        return None
    result = set(expected)
    if service == "identity-admission":
        result.update(_IDENTITY_OPTIONAL_SECRETS.values())
    if notification_publication_enabled and service in {
        "identity-api",
        "identity-notification-publisher",
    }:
        result.add("notification_publication_token")
    if jobs_queue_enabled and service == "identity-api":
        result.add("jobs_queue_token")
    return result


def validate_jobs_queue_image_inputs(
    image: str | None,
    identity: str | None,
    source_revision: str | None,
    token: str | None,
) -> bool:
    """Require one immutable, source-bound image and its distinct service token."""
    values = (image, identity, source_revision, token)
    if all(value is None for value in values):
        return False
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("Jobs queue runtime inputs are incomplete")
    assert image is not None and identity is not None
    assert source_revision is not None and token is not None
    if (
        _JOBS_QUEUE_IMAGE_ID.fullmatch(image) is None
        or _JOBS_QUEUE_ACCEPTANCE_ID.fullmatch(identity) is None
        or _JOBS_QUEUE_SOURCE_REVISION.fullmatch(source_revision) is None
    ):
        raise ValueError("Jobs queue runtime image provenance is invalid")
    try:
        token_bytes = token.encode("ascii")
    except UnicodeError:
        raise ValueError("Jobs queue service token is invalid") from None
    if (
        not token_bytes
        or len(token_bytes) > 4096
        or any(value < 33 or value > 126 for value in token_bytes)
    ):
        raise ValueError("Jobs queue service token is invalid")
    return True


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
        publisher_redis_url: str,
        smtp_port: int,
        notifications_socket_image: str | None = None,
        notifications_socket_identity: str | None = None,
        notification_delivery_database_url: str | None = None,
        identity_actor_status_database_url: str | None = None,
        notifications_socket_redis_url: str | None = None,
        admission_token: str | None = None,
        notifications_publisher_image: str | None = None,
        notifications_publisher_identity: str | None = None,
        notification_publication_token: str | None = None,
        notification_api_redis_url: str | None = None,
        jobs_queue_image: str | None = None,
        jobs_queue_identity: str | None = None,
        jobs_queue_source_revision: str | None = None,
        jobs_queue_token: str | None = None,
    ) -> None:
        self.test_engine = test_engine
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
        notification_inputs = (
            notifications_socket_image,
            notifications_socket_identity,
            notification_delivery_database_url,
            identity_actor_status_database_url,
            notifications_socket_redis_url,
            admission_token,
        )
        if any(value is not None for value in notification_inputs) and not all(
            isinstance(value, str) and value.strip() for value in notification_inputs
        ):
            raise ValueError("Notifications socket verifier inputs are incomplete")
        self._notifications_socket_enabled = all(
            value is not None for value in notification_inputs
        )
        self._notifications_socket_image = notifications_socket_image
        self._notifications_socket_identity = notifications_socket_identity
        self._admission_token = admission_token
        publication_inputs = (
            notifications_publisher_image,
            notifications_publisher_identity,
            notification_publication_token,
            notification_api_redis_url,
        )
        if any(value is not None for value in publication_inputs) and not all(
            isinstance(value, str) and value.strip() for value in publication_inputs
        ):
            raise ValueError("Notifications publication verifier inputs are incomplete")
        self._notification_publication_enabled = all(
            value is not None for value in publication_inputs
        )
        if (
            notification_publication_token is not None
            and notification_publication_token
            in {secret, api_secret, control, admission_token}
        ):
            raise ValueError("Notifications publication credential must be distinct")
        self._notifications_publisher_image = notifications_publisher_image
        self._notifications_publisher_identity = notifications_publisher_identity
        self._notification_publication_token = notification_publication_token
        self._jobs_queue_enabled = validate_jobs_queue_image_inputs(
            jobs_queue_image,
            jobs_queue_identity,
            jobs_queue_source_revision,
            jobs_queue_token,
        )
        self._jobs_queue_image = jobs_queue_image
        self._jobs_queue_identity = jobs_queue_identity
        self._jobs_queue_source_revision = jobs_queue_source_revision
        self._jobs_queue_token = jobs_queue_token
        if jobs_queue_token is not None and jobs_queue_token in {
            secret,
            api_secret,
            control,
            mail_key,
            admission_token,
            notification_publication_token,
        }:
            raise ValueError("Jobs queue bearer must be distinct from other secrets")
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
        self._publisher_redis_url = self._docker_url(publisher_redis_url)
        if notification_api_redis_url is not None:
            api_redis = make_url(notification_api_redis_url)
            reference_redis = make_url(self._required_environment("REDIS_URL"))
            if (
                (
                    api_redis.drivername,
                    api_redis.host,
                    api_redis.port,
                    api_redis.database,
                )
                != (
                    reference_redis.drivername,
                    reference_redis.host,
                    reference_redis.port,
                    reference_redis.database,
                )
                or not api_redis.username
                or not api_redis.password
                or api_redis.username
                in {
                    make_url(login_redis_url).username,
                    make_url(publisher_redis_url).username,
                }
            ):
                raise ValueError(
                    "API Redis credential does not match the owned scoped endpoint"
                )
        self._notification_api_redis_url = (
            self._docker_url(notification_api_redis_url)
            if notification_api_redis_url is not None
            else None
        )
        self._jobs_queue_broker_url = None
        if self._jobs_queue_enabled:
            broker_url = make_url(
                os.environ.get("CELERY_BROKER_URL")
                or self._required_environment("REDIS_URL")
            )
            redis_owner_url = make_url(self._required_environment("REDIS_URL"))
            if (
                broker_url.drivername,
                broker_url.host,
                broker_url.port,
                broker_url.database,
            ) != (
                redis_owner_url.drivername,
                redis_owner_url.host,
                redis_owner_url.port,
                redis_owner_url.database,
            ):
                raise ValueError(
                    "Jobs queue broker must use the verifier-owned Redis DB"
                )
            if not broker_url.password:
                raise ValueError("Jobs queue broker credentials are required")
            self._jobs_queue_broker_url = self._docker_url(
                broker_url.render_as_string(hide_password=False)
            )
            if jobs_queue_token == broker_url.password:
                raise ValueError(
                    "Jobs queue bearer must be distinct from broker credentials"
                )
        self._notifications_socket_redis_url = (
            self._docker_url(notifications_socket_redis_url)
            if notifications_socket_redis_url is not None
            else None
        )
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
        self._database_urls: dict[str, URL] = {
            kind: make_url(_dsn(kind, test_engine)).set(
                host=self._postgres_name, port=5432
            )
            for kind in _DATABASES
        }
        self._database_name = str(test_engine.url.database or "")
        self._validate_database_name(self._database_name)
        self._notifications_socket_database_url = (
            verifier_database_url(
                notification_delivery_database_url,
                test_engine.url,
                expected_role="notification_delivery_read_client",
                container_name=self._postgres_name,
            )
            if notification_delivery_database_url is not None
            else None
        )
        self._actor_status_database_url = (
            verifier_database_url(
                identity_actor_status_database_url,
                test_engine.url,
                expected_role="identity_actor_status_client",
                container_name=self._postgres_name,
            )
            if identity_actor_status_database_url is not None
            else None
        )
        api_host_url = make_url(
            self._required_environment("AAP_MODULE_DATA_IDENTITY_API_CONSUMER_URL")
        )
        self._api_database_url = make_url(
            self._required_environment(
                "AAP_MODULE_DATA_IDENTITY_API_CONSUMER_CONTAINER_URL"
            )
        )
        if (
            (api_host_url.host, api_host_url.port, api_host_url.database)
            != (test_engine.url.host, test_engine.url.port, test_engine.url.database)
            or api_host_url.username != "identity_api_consumer"
            or api_host_url.database is None
            or not api_host_url.database.endswith("_test")
            or self._api_database_url.host != "host.docker.internal"
            or self._api_database_url.port != api_host_url.port
            or self._api_database_url.database != api_host_url.database
            or self._api_database_url.username != api_host_url.username
            or self._api_database_url.password != api_host_url.password
        ):
            raise RuntimeError("Verifier API consumer database identity did not match")
        self._api_database_url = self._api_database_url.set(
            host=self._postgres_name, port=5432
        )
        self._service_ids: dict[str, str] = {}
        self._inspection_evidence: dict[str, dict] = {}
        self._services = (
            _SERVICES
            + (
                (_NOTIFICATIONS_SOCKET_SERVICE,)
                if self._notifications_socket_enabled
                else ()
            )
            + ((_JOBS_QUEUE_SERVICE,) if self._jobs_queue_enabled else ())
        )
        self._api_origin = ""
        self._identity_origin = ""
        self._verify_image(self._identity_image, self._identity)
        self._verify_image(self._api_image, self._api_identity)
        if getattr(self, "_notification_publication_enabled", False):
            self._verify_image(
                self._notifications_publisher_image or "",
                self._notifications_publisher_identity or "",
                acceptance=_NOTIFICATIONS_PUBLISHER_IMAGE_ACCEPTANCE,
                expected_user="10004:10004",
            )
        if self._notifications_socket_enabled:
            self._verify_image(
                self._notifications_socket_image or "",
                self._notifications_socket_identity or "",
                acceptance=_NOTIFICATIONS_SOCKET_IMAGE_ACCEPTANCE,
                expected_user="10004:10004",
            )
        if self._jobs_queue_enabled:
            self._verify_image(
                self._jobs_queue_image or "",
                self._jobs_queue_identity or "",
                acceptance=_JOBS_QUEUE_IMAGE_ACCEPTANCE,
                expected_user="10005:10005",
                expected_source_revision=self._jobs_queue_source_revision,
            )

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

    def redact(self, value: str) -> str:
        values = [
            self._secret,
            self._api_secret,
            self._control,
            self._mail_key,
            self._admission_token or "",
            getattr(self, "_notification_publication_token", None) or "",
            getattr(self, "_jobs_queue_token", None) or "",
        ]
        urls = [self._api_database_url, *self._database_urls.values()]
        if self._notifications_socket_database_url is not None:
            urls.append(self._notifications_socket_database_url)
        if self._actor_status_database_url is not None:
            urls.append(self._actor_status_database_url)
        for url in urls:
            values.extend(
                [url.render_as_string(hide_password=False), url.password or ""]
            )
        values.extend(
            [
                self._login_redis_url,
                self._publisher_redis_url,
                self._notifications_socket_redis_url or "",
                getattr(self, "_notification_api_redis_url", None) or "",
                getattr(self, "_jobs_queue_broker_url", None) or "",
                (
                    make_url(self._jobs_queue_broker_url).password or ""
                    if self._jobs_queue_broker_url
                    else ""
                ),
                os.environ.get("AAP_MODULE_DATA_API_BOOTSTRAP_S3_SECRET_KEY", ""),
            ]
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
        if not self._compose_env_file.is_file() or not self._override_file.is_file():
            return
        containers = self._verify_project_ownership()
        if containers:
            self._compose("stop", timeout=90)
        self._disconnect_dependencies()
        if containers:
            self._compose("down", timeout=90)
        self._service_ids = {}

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
        return name

    def _connect_dependencies(self):
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        network = self._dependency_network()
        if network is None:
            raise RuntimeError("Compose did not create its owned network")
        for container in (self._postgres_container, self._redis_name):
            details = self._inspect(container)
            if network not in details["NetworkSettings"]["Networks"]:
                self._docker(
                    ["network", "connect", network, container],
                    operation="owned dependency attachment",
                )

    def _disconnect_dependencies(self):
        network = self._dependency_network()
        if network is None:
            return
        _verify_postgres(self.test_engine.url)
        _verify_redis()
        for container in (self._postgres_container, self._redis_name):
            details = self._inspect(container)
            if network in details["NetworkSettings"]["Networks"]:
                self._docker(
                    ["network", "disconnect", network, container],
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

        for kind, (_, secret_name) in _DATABASES.items():
            url = self._database_urls[kind].set(database=self._database_name)
            self._write_secret(secret_name, url.render_as_string(hide_password=False))

        for env_name, secret_name in (
            ("IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE", "identity_recovery_mail_key"),
            ("IDENTITY_RUNTIME_LOGIN_REDIS_URL_FILE", "identity_login_redis_url"),
            ("IDENTITY_RUNTIME_TOKEN_FILE", "identity_token"),
            ("IDENTITY_RUNTIME_SIGNING_SECRET_FILE", "identity_signing_secret"),
            (
                "IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE",
                "notification_publish_redis_url",
            ),
        ):
            value = {
                "identity_recovery_mail_key": self._mail_key,
                "identity_login_redis_url": self._login_redis_url,
                "identity_token": self._control,
                "identity_signing_secret": self._secret,
                "notification_publish_redis_url": self._publisher_redis_url,
            }[secret_name]
            self._write_secret(secret_name, value)

        if self._notifications_socket_enabled:
            assert self._notifications_socket_database_url is not None
            assert self._actor_status_database_url is not None
            assert self._notifications_socket_redis_url is not None
            assert self._admission_token is not None
            self._write_secret(
                "identity_actor_status_database_url",
                self._actor_status_database_url.set(
                    database=self._database_name
                ).render_as_string(hide_password=False),
            )
            self._write_secret("identity_admission_token", self._admission_token)
            self._write_secret(
                "notification_socket_database_url",
                self._notifications_socket_database_url.set(
                    database=self._database_name
                ).render_as_string(hide_password=False),
            )
            self._write_secret(
                "notification_socket_redis_url", self._notifications_socket_redis_url
            )
            self._write_secret(
                "notification_socket_identity_token", self._admission_token
            )
        if getattr(self, "_notification_publication_enabled", False):
            self._write_secret(
                "notification_publication_token",
                self._notification_publication_token,
            )
        if self._jobs_queue_enabled:
            assert self._jobs_queue_token is not None
            assert self._jobs_queue_broker_url is not None
            self._write_secret("jobs_queue_token", self._jobs_queue_token)
            self._write_secret("jobs_queue_broker_url", self._jobs_queue_broker_url)

        self._write_environment_file(self._api_env_file, self._api_environment())
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
            "IDENTITY_MAIL_SMTP_USER": "",
            "IDENTITY_MAIL_SMTP_PASSWORD": "",
            "IDENTITY_MAIL_SMTP_FROM": "noreply@example.test",
        }
        if getattr(self, "_notification_publication_enabled", False):
            compose_values.update(
                {
                    "AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE": self._notifications_publisher_image,
                    "NOTIFICATION_PUBLICATION_TOKEN_SECRET_FILE": str(
                        self._secret_directory / "notification_publication_token"
                    ),
                }
            )
        compose_values.update(
            _identity_optional_secret_selectors(
                self._secret_directory, self._notifications_socket_enabled
            )
        )
        for kind, (environment_name, secret_name) in _DATABASES.items():
            compose_values[environment_name] = str(self._secret_directory / secret_name)
        for env_name, secret_name in (
            ("IDENTITY_RUNTIME_RECOVERY_MAIL_KEY_FILE", "identity_recovery_mail_key"),
            ("IDENTITY_RUNTIME_LOGIN_REDIS_URL_FILE", "identity_login_redis_url"),
            ("IDENTITY_RUNTIME_TOKEN_FILE", "identity_token"),
            ("IDENTITY_RUNTIME_SIGNING_SECRET_FILE", "identity_signing_secret"),
            (
                "IDENTITY_RUNTIME_NOTIFICATION_PUBLISH_REDIS_URL_FILE",
                "notification_publish_redis_url",
            ),
        ):
            compose_values[env_name] = str(self._secret_directory / secret_name)
        if self._notifications_socket_enabled:
            compose_values.update(
                {
                    "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IMAGE": (
                        self._notifications_socket_image or ""
                    ),
                    "AAP_NOTIFICATIONS_SOCKET_RUNTIME_IDENTITY": (
                        self._notifications_socket_identity or ""
                    ),
                    "NOTIFICATION_SOCKET_DATABASE_URL_FILE": str(
                        self._secret_directory / "notification_socket_database_url"
                    ),
                    "NOTIFICATION_SOCKET_REDIS_URL_FILE": str(
                        self._secret_directory / "notification_socket_redis_url"
                    ),
                    "NOTIFICATION_SOCKET_IDENTITY_URL": (
                        "http://identity-admission:8170"
                    ),
                    "NOTIFICATION_SOCKET_IDENTITY_TOKEN_FILE": str(
                        self._secret_directory / "notification_socket_identity_token"
                    ),
                }
            )
        if self._jobs_queue_enabled:
            compose_values.update(
                {
                    "AAP_JOBS_QUEUE_RUNTIME_IMAGE": self._jobs_queue_image,
                    "AAP_JOBS_QUEUE_RUNTIME_IDENTITY": self._jobs_queue_identity,
                    "AAP_JOBS_QUEUE_RUNTIME_SOURCE_REVISION": self._jobs_queue_source_revision,
                    "JOBS_QUEUE_BROKER_URL_SECRET_FILE": str(
                        self._secret_directory / "jobs_queue_broker_url"
                    ),
                    "JOBS_QUEUE_TOKEN_SECRET_FILE": str(
                        self._secret_directory / "jobs_queue_token"
                    ),
                }
            )
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
            "IDENTITY_URL": "http://identity-admission:8170",
            "IDENTITY_TOKEN": self._control,
            "NOTIFICATION_SOCKET_URL": "",
            "NOTIFICATION_PUBLICATION_URL": "",
            "NOTIFICATION_PUBLICATION_TOKEN": "",
            "NOTIFICATION_PUBLICATION_TOKEN_FILE": "",
            "JOBS_QUEUE_URL": "",
            "JOBS_QUEUE_TOKEN": "",
            "JOBS_QUEUE_TOKEN_FILE": "",
            "ALEMBIC_AUTO_UPGRADE": "false",
            "E2E_SEED_ENABLED": "false",
            "AUDIT_ASYNC": "true",
            "MIGRATION_DATABASE_URL": "",
            "ALLOW_OPEN_REGISTRATION": "false",
            "TURNSTILE_ENABLED": "false",
            "SENTRY_DSN": "",
            "DEBUG": "false",
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
        if self._notifications_socket_enabled:
            values["NOTIFICATION_SOCKET_URL"] = "ws://notifications-socket:8180"
        if getattr(self, "_notification_publication_enabled", False):
            values.pop("NOTIFICATION_PUBLICATION_TOKEN")
            values.update(
                {
                    "NOTIFICATION_PUBLICATION_URL": "http://identity-notification-publisher:8181",
                    "NOTIFICATION_PUBLICATION_TOKEN_FILE": "/run/secrets/notification_publication_token",
                    "REDIS_URL": self._notification_api_redis_url,
                    "CELERY_BROKER_URL": self._notification_api_redis_url,
                }
            )
        if self._jobs_queue_enabled:
            values.pop("JOBS_QUEUE_TOKEN")
            values.update(
                {
                    "JOBS_QUEUE_URL": "http://jobs-queue:8190",
                    "JOBS_QUEUE_TOKEN_FILE": "/run/secrets/jobs_queue_token",
                }
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
        values["MINIO_PUBLIC_URL"] = (
            self._docker_http_url(public_url)
            if public_url.startswith("http")
            else public_url
        )
        for name in _API_BINDING_NAMES + _API_DATABASE_FILE_NAMES:
            values[name] = ""
        values["IDENTITY_RECOVERY_MAIL_KEY"] = ""
        values["IDENTITY_RECOVERY_MAIL_KEY_FILE"] = ""
        return values

    def _write_override_file(self) -> None:
        api_env_file = json.dumps(str(self._api_env_file))
        publisher_image = "${AAP_IDENTITY_RUNTIME_IMAGE:?}"
        publisher_overrides = ""
        api_secret_names = []
        publication_dependency = ""
        secret_definitions = []
        if getattr(self, "_notification_publication_enabled", False):
            publisher_image = "${AAP_NOTIFICATIONS_DELIVERY_RUNTIME_IMAGE:?}"
            publisher_overrides = """
    command: ["aap-notifications-publisher"]
    user: "10004:10004"
    environment:
      NOTIFICATION_PUBLISH_API_TOKEN_FILE: /run/secrets/notification_publication_token
      NOTIFICATION_PUBLISH_HOST: 0.0.0.0
      NOTIFICATION_PUBLISH_PORT: "8181"
    secrets: !override
      - notification_publish_database_url
      - notification_publish_redis_url
      - notification_publication_token
    healthcheck:
      disable: false
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8181/health/ready', timeout=2)"]
      interval: 1s
      timeout: 3s
      start_period: 3s
      retries: 30
"""
            api_secret_names.append("notification_publication_token")
            publication_dependency = """
      identity-notification-publisher:
        condition: service_healthy
"""
            secret_definitions.append(
                """  notification_publication_token:
    file: ${NOTIFICATION_PUBLICATION_TOKEN_SECRET_FILE:?}"""
            )
        socket_service = ""
        socket_dependency = ""
        if self._notifications_socket_enabled:
            socket_service = """
  notifications-socket:
    image: ${AAP_NOTIFICATIONS_SOCKET_RUNTIME_IMAGE:?}
    build: !reset null
    depends_on:
      identity-admission:
        condition: service_healthy
    labels: *identity-module-owner-labels
"""
            socket_dependency = """
      notifications-socket:
        condition: service_healthy
"""
        jobs_queue_service = ""
        jobs_queue_dependency = ""
        if self._jobs_queue_enabled:
            jobs_queue_service = """
  jobs-queue:
    image: ${AAP_JOBS_QUEUE_RUNTIME_IMAGE:?}
    build: !reset null
    command: ["aap-jobs-queue"]
    user: "10005:10005"
    environment:
      JOBS_QUEUE_BROKER_URL_FILE: /run/secrets/jobs_queue_broker_url
      JOBS_QUEUE_TOKEN_FILE: /run/secrets/jobs_queue_token
      JOBS_QUEUE_HOST: 0.0.0.0
      JOBS_QUEUE_PORT: "8190"
    secrets: !override
      - jobs_queue_broker_url
      - jobs_queue_token
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    pids_limit: 64
    mem_limit: 256m
    cpus: 1
    healthcheck:
      disable: false
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8190/health/ready', timeout=2)"]
      interval: 1s
      timeout: 3s
      start_period: 3s
      retries: 30
    labels: *identity-module-owner-labels
"""
            jobs_queue_dependency = """
      jobs-queue:
        condition: service_healthy
"""
            api_secret_names.append("jobs_queue_token")
            secret_definitions.extend(
                [
                    """  jobs_queue_broker_url:
    file: ${JOBS_QUEUE_BROKER_URL_SECRET_FILE:?}""",
                    """  jobs_queue_token:
    file: ${JOBS_QUEUE_TOKEN_SECRET_FILE:?}""",
                ]
            )
        api_secrets = (
            "    secrets:\n" + "".join(f"      - {name}\n" for name in api_secret_names)
            if api_secret_names
            else ""
        )
        secrets_block = (
            "secrets:\n" + "\n".join(secret_definitions) + "\n"
            if secret_definitions
            else ""
        )
        self._override_file.write_text(
            f"""services:
  identity-admission:
    image: ${{AAP_IDENTITY_RUNTIME_IMAGE:?}}
    build: !reset null
    labels: &identity-module-owner-labels
      org.aap.acceptance: module-data-boundary
      org.aap.acceptance-id: ${{IDENTITY_MODULE_STACK_ID:?}}
    ports: !override
      - "127.0.0.1::8170"
  identity-mail:
    image: ${{AAP_IDENTITY_RUNTIME_IMAGE:?}}
    build: !reset null
    labels: *identity-module-owner-labels
  identity-mail-v2:
    image: ${{AAP_IDENTITY_RUNTIME_IMAGE:?}}
    build: !reset null
    labels: *identity-module-owner-labels
  identity-notification-publisher:
    image: {publisher_image}
    build: !reset null
    labels: *identity-module-owner-labels
{publisher_overrides.rstrip()}
{jobs_queue_service.rstrip()}
  identity-api:
    image: ${{AAP_IDENTITY_API_RUNTIME_IMAGE:?}}
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--timeout-graceful-shutdown", "3"]
    user: "10003:10003"
    env_file:
      - {api_env_file}
    depends_on:
      identity-admission:
        condition: service_healthy
{socket_dependency.rstrip()}
{publication_dependency.rstrip()}
{jobs_queue_dependency.rstrip()}
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
{socket_service.rstrip()}
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
        if self._notifications_socket_enabled:
            arguments.extend(["-f", str(ROOT / "docker-compose.notifications.yml")])
        arguments.extend(["-f", str(self._override_file), "--profile", "mail-v2"])
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
        self._identity_origin = self._published_origin("identity-admission", 8170)
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

    async def _wait_publisher_running(self, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            containers = self._verify_project_ownership()
            details = containers.get("identity-notification-publisher")
            if details:
                state = details.get("State") or {}
                if state.get("Running") is True and (
                    not getattr(self, "_notification_publication_enabled", False)
                    or (state.get("Health") or {}).get("Status") == "healthy"
                ):
                    return
            await asyncio.sleep(0.2)
        self._write_log("identity-notification-publisher: did not remain running")
        raise RuntimeError(
            "Owned Identity notification publisher did not remain running; "
            f"see {self._log_file}"
        )

    async def _wait_service_ready(self, service: str) -> None:
        if service == "identity-admission":
            await self._wait_http(self._identity_origin + "/ready", service)
        elif service == "identity-api":
            await self._wait_http(self._api_origin + "/health/db", service)
        elif service in {"identity-mail", "identity-mail-v2"}:
            await self._wait_mail_ready(service)
        elif service == "identity-notification-publisher":
            await self._wait_publisher_running()
        elif service == _NOTIFICATIONS_SOCKET_SERVICE:
            await self._wait_notifications_socket_ready()
        elif service == _JOBS_QUEUE_SERVICE:
            await self._wait_jobs_queue_ready()

    async def _wait_ready(self) -> None:
        for service in self._services:
            await self._wait_service_ready(service)

    async def _wait_notifications_socket_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = self._verify_project_ownership().get(
                _NOTIFICATIONS_SOCKET_SERVICE
            )
            if details is not None:
                state = details.get("State") or {}
                health = (state.get("Health") or {}).get("Status")
                if state.get("Running") is True and health == "healthy":
                    return
                if state.get("Running") is False and state.get("Status") == "exited":
                    break
            await asyncio.sleep(0.2)
        self._write_log("notifications-socket: readiness timed out")
        raise RuntimeError(
            f"Owned Notifications socket did not become ready; see {self._log_file}"
        )

    async def _wait_jobs_queue_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            details = self._verify_project_ownership().get(_JOBS_QUEUE_SERVICE)
            if details is not None:
                state = details.get("State") or {}
                health = (state.get("Health") or {}).get("Status")
                if state.get("Running") is True and health == "healthy":
                    return
                if state.get("Running") is False and state.get("Status") == "exited":
                    break
            await asyncio.sleep(0.2)
        self._write_log("jobs-queue: readiness timed out")
        raise RuntimeError(
            f"Owned Jobs queue did not become ready; see {self._log_file}"
        )

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
            if service == _JOBS_QUEUE_SERVICE:
                network_names = set(
                    (details.get("NetworkSettings", {}).get("Networks") or {}).keys()
                )
                if network_names != {f"{self.project_name}_default"}:
                    raise RuntimeError(
                        "Jobs queue is outside its owned private network"
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
        if service == _JOBS_QUEUE_SERVICE:
            expected_user = "10005:10005"
        elif service == _NOTIFICATIONS_SOCKET_SERVICE or (
            service == "identity-notification-publisher"
            and getattr(self, "_notification_publication_enabled", False)
        ):
            expected_user = "10004:10004"
        else:
            expected_user = "10003:10003"
        if config.get("User") != expected_user:
            raise RuntimeError(f"{service} does not run as UID/GID {expected_user}")
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

        targets = {
            mount.get("Destination")
            for mount in mounts
            if str(mount.get("Destination", "")).startswith("/run/secrets/")
        }
        expected = _expected_secret_mount_names(
            service,
            self._notifications_socket_enabled,
            getattr(self, "_notification_publication_enabled", False),
            self._jobs_queue_enabled,
        )
        if expected is None or targets != {f"/run/secrets/{name}" for name in expected}:
            raise RuntimeError(f"{service} secret mounts cross service boundaries")
        if service == _JOBS_QUEUE_SERVICE:
            actual_targets = {
                mount.get("Destination") for mount in mounts if mount.get("Destination")
            }
            if actual_targets != targets:
                raise RuntimeError("Jobs queue has an unexpected runtime mount")
            if any(
                mount.get("RW") is not False
                for mount in mounts
                if mount.get("Destination") in targets
            ):
                raise RuntimeError("Jobs queue secret mounts must be read-only")
        for mount in mounts:
            source = mount.get("Source")
            if source and Path(source).resolve().is_relative_to(ROOT):
                raise RuntimeError(f"{service} mounts the application source tree")

        if service == "identity-admission":
            expected_optional_selectors = {
                "IDENTITY_ADMISSION_TOKEN_FILE": (
                    "/run/secrets/identity_admission_token"
                    if self._notifications_socket_enabled
                    else ""
                ),
                "IDENTITY_ACTOR_STATUS_DATABASE_URL_FILE": (
                    "/run/secrets/identity_actor_status_database_url"
                    if self._notifications_socket_enabled
                    else ""
                ),
            }
            if any(
                env.get(name, "") != value
                for name, value in expected_optional_selectors.items()
            ):
                raise RuntimeError("Identity optional credential selectors differ")
            if not self._notifications_socket_enabled and any(
                (self._secret_directory / name).exists()
                for name in _IDENTITY_OPTIONAL_SECRETS.values()
            ):
                raise RuntimeError("Disabled Identity secret files contain credentials")
        elif service == "identity-api":
            if (
                "IDENTITY_SIGNING_SECRET" in env
                or "IDENTITY_SIGNING_SECRET_FILE" in env
                or any(value == self._secret for value in env.values())
            ):
                raise RuntimeError("API runtime environment contains the owner signer")
            if (
                env.get("SECRET_KEY") != self._api_secret
                or env.get("IDENTITY_TOKEN") != self._control
            ):
                raise RuntimeError(
                    "API runtime does not have its explicit edge credentials"
                )
            if any(
                env.get(name, "")
                for name in _API_BINDING_NAMES + _API_DATABASE_FILE_NAMES
            ):
                raise RuntimeError(
                    "API runtime contains a direct Identity database binding"
                )
            expected_socket_origin = (
                "ws://notifications-socket:8180"
                if self._notifications_socket_enabled
                else ""
            )
            if env.get("NOTIFICATION_SOCKET_URL", "") != expected_socket_origin:
                raise RuntimeError("API runtime socket owner binding does not match")
            expected_publication_origin = (
                "http://identity-notification-publisher:8181"
                if getattr(self, "_notification_publication_enabled", False)
                else ""
            )
            expected_publication_token_file = (
                "/run/secrets/notification_publication_token"
                if getattr(self, "_notification_publication_enabled", False)
                else ""
            )
            if (
                env.get("NOTIFICATION_PUBLICATION_URL", "")
                != expected_publication_origin
                or env.get("NOTIFICATION_PUBLICATION_TOKEN", "")
                or env.get("NOTIFICATION_PUBLICATION_TOKEN_FILE", "")
                != expected_publication_token_file
                or (
                    getattr(self, "_notification_publication_enabled", False)
                    and any(
                        env.get(name) != self._notification_api_redis_url
                        for name in ("REDIS_URL", "CELERY_BROKER_URL")
                    )
                )
            ):
                raise RuntimeError(
                    "API publication binding crosses credential boundaries"
                )
            expected_jobs_url = (
                "http://jobs-queue:8190" if self._jobs_queue_enabled else ""
            )
            expected_jobs_token_file = (
                "/run/secrets/jobs_queue_token" if self._jobs_queue_enabled else ""
            )
            if (
                env.get("JOBS_QUEUE_URL", "") != expected_jobs_url
                or env.get("JOBS_QUEUE_TOKEN", "")
                or env.get("JOBS_QUEUE_TOKEN_FILE", "") != expected_jobs_token_file
                or (
                    self._jobs_queue_enabled
                    and any(value == self._jobs_queue_token for value in env.values())
                )
            ):
                raise RuntimeError(
                    "API Jobs queue binding crosses credential boundaries"
                )
            if self._jobs_queue_enabled and any(
                mount.get("RW") is not False
                for mount in mounts
                if mount.get("Destination") == "/run/secrets/jobs_queue_token"
            ):
                raise RuntimeError("API Jobs queue bearer mount must be read-only")
        elif service == _JOBS_QUEUE_SERVICE:
            expected_environment = {
                "JOBS_QUEUE_BROKER_URL_FILE": "/run/secrets/jobs_queue_broker_url",
                "JOBS_QUEUE_TOKEN_FILE": "/run/secrets/jobs_queue_token",
                "JOBS_QUEUE_HOST": "0.0.0.0",
                "JOBS_QUEUE_PORT": "8190",
            }
            if any(
                env.get(name) != value for name, value in expected_environment.items()
            ):
                raise RuntimeError("Jobs queue credential selectors differ")
            forbidden = (
                "DATABASE",
                "IDENTITY",
                "API_",
                "SECRET",
                "MINIO",
                "REDIS_URL",
                "CELERY_BROKER_URL",
            )
            if any(any(part in name for part in forbidden) for name in env):
                raise RuntimeError("Jobs queue received a domain or API credential")
            if any(
                value in env.values()
                for value in (
                    self._jobs_queue_token,
                    self._jobs_queue_broker_url,
                    self._api_secret,
                    self._control,
                    self._secret,
                )
            ):
                raise RuntimeError("Jobs queue runtime environment contains a secret")
            if env.get("JOBS_QUEUE_BROKER_URL") or env.get("JOBS_QUEUE_TOKEN"):
                raise RuntimeError("Jobs queue credentials must be mounted as files")
            if any(bindings for bindings in (host.get("PortBindings") or {}).values()):
                raise RuntimeError("Jobs queue must not publish a host port")
            if state.get("Running") is True and (
                (state.get("Health") or {}).get("Status") != "healthy"
            ):
                raise RuntimeError("Jobs queue broker readiness failed")
        elif service == "identity-notification-publisher" and getattr(
            self, "_notification_publication_enabled", False
        ):
            if (
                env.get("NOTIFICATION_PUBLISH_API_TOKEN_FILE")
                != "/run/secrets/notification_publication_token"
                or env.get("NOTIFICATION_PUBLISH_HOST") != "0.0.0.0"
                or env.get("NOTIFICATION_PUBLISH_PORT") != "8181"
                or self._notification_publication_token in env.values()
            ):
                raise RuntimeError("Publisher API credential binding differs")
            if any(bindings for bindings in (host.get("PortBindings") or {}).values()):
                raise RuntimeError(
                    "Notifications publisher must not publish a host port"
                )
        elif service == _NOTIFICATIONS_SOCKET_SERVICE:
            expected_environment = {
                "NOTIFICATION_SOCKET_DATABASE_URL_FILE": (
                    "/run/secrets/notification_socket_database_url"
                ),
                "NOTIFICATION_SOCKET_REDIS_URL_FILE": (
                    "/run/secrets/notification_socket_redis_url"
                ),
                "NOTIFICATION_SOCKET_IDENTITY_URL": ("http://identity-admission:8170"),
                "NOTIFICATION_SOCKET_IDENTITY_TOKEN_FILE": (
                    "/run/secrets/notification_socket_identity_token"
                ),
            }
            if any(
                env.get(name) != value for name, value in expected_environment.items()
            ):
                raise RuntimeError("Notifications socket secret file binding differs")
            secret_values = (
                self._admission_token,
                self._notifications_socket_database_url.render_as_string(
                    hide_password=False
                ),
                self._notifications_socket_redis_url,
            )
            if any(value and value in env.values() for value in secret_values):
                raise RuntimeError("Notifications socket environment contains a secret")
            port_bindings = host.get("PortBindings") or {}
            if any(bindings for bindings in port_bindings.values()):
                raise RuntimeError("Notifications socket must not publish a host port")

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

    @staticmethod
    def _docker_endpoint(value: str) -> str:
        parsed = urlsplit(value if "://" in value else "http://" + value)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Verifier supplied an invalid object storage endpoint")
        port = parsed.port
        return (
            f"host.docker.internal:{port}"
            if port is not None
            else "host.docker.internal"
        )

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
        endpoint = cls._docker_endpoint(f"{parsed.scheme}://{parsed.netloc}")
        return f"{parsed.scheme}://{endpoint}{parsed.path}"
