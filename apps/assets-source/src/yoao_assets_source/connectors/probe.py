"""Bounded connector probes shared by connection-management runtimes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from threading import Timer
from typing import Any

from .host_guard import assert_host_allowed, normalize_allowlist
from .registry import target_host

_SAMPLE_LIMIT = 20
PROBE_CONNECT_TIMEOUT = 10
PROBE_TIMEOUT_SECONDS = 10


def _mark_unsettled(exc: BaseException) -> BaseException:
    exc.storage_operation_unsettled = True
    return exc


def _close_registered(resource) -> None:
    """A failed close is not proof that the physical handle ended."""
    close = getattr(resource, "close", None)
    if close is None:
        return
    try:
        close()
    except BaseException as exc:
        raise _mark_unsettled(exc)


def _close_all(*resources) -> None:
    """Close every handle, then surface the first uncertain close."""
    failures: list[BaseException] = []
    for resource in resources:
        if resource is None:
            continue
        close = getattr(resource, "close", None)
        if close is None:
            continue
        try:
            close()
        except BaseException as exc:
            failures.append(_mark_unsettled(exc))
    if failures:
        raise failures[0]


@contextmanager
def _close_on_deadline(close):
    """Close the transport if the remote listing stops yielding; wait for it."""
    failures: list[BaseException] = []

    def finish_on_deadline():
        try:
            close()
        except BaseException as exc:
            failures.append(_mark_unsettled(exc))

    timer = Timer(PROBE_TIMEOUT_SECONDS, finish_on_deadline)
    timer.daemon = True
    timer.start()
    try:
        yield
    finally:
        timer.cancel()
        timer.join()
        if failures:
            raise failures[0]


async def probe_connection(
    kind: str,
    config: dict[str, Any],
    secret: dict[str, Any],
    *,
    allowlist: list[str],
    known_hosts_file: str | None = None,
) -> tuple[bool, str, int | None]:
    """Check a connector and count a small sample without exposing SDK errors."""
    normalized_allowlist = normalize_allowlist(allowlist)
    await asyncio.to_thread(
        assert_host_allowed, target_host(kind, config), normalized_allowlist
    )

    try:
        # AnyIO and the connector SDKs are runtime-only dependencies. Keep them
        # out of imports for applications that use just the portable contracts.
        import anyio

        count = await anyio.to_thread.run_sync(
            _probe_blocking,
            kind,
            dict(config or {}),
            dict(secret or {}),
            known_hosts_file,
        )
        return True, "连接成功", count
    except Exception as exc:  # noqa: BLE001 - never expose SDK text or endpoints
        return False, f"连接失败: {_safe_exception_class(exc)}", None


def _safe_exception_class(exc: Exception) -> str:
    name = type(exc).__name__
    if len(name) <= 128 and name.isascii() and name.isidentifier():
        return name
    return "Exception"


def _probe_blocking(
    kind: str,
    config: dict[str, Any],
    secret: dict[str, Any],
    known_hosts_file: str | None,
) -> int:
    if kind == "s3":
        return _probe_s3(config, secret)
    return _probe_sftp(config, secret, known_hosts_file)


def _probe_s3(config: dict[str, Any], secret: dict[str, Any]) -> int:
    import boto3
    from botocore.config import Config as BotoConfig

    scheme = "https" if config.get("use_ssl") else "http"
    endpoint = config["endpoint"]
    if "://" not in endpoint:
        endpoint = f"{scheme}://{endpoint}"
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=secret.get("access_key") or "",
        aws_secret_access_key=secret.get("secret_key") or "",
        region_name=config.get("region"),
        config=BotoConfig(
            connect_timeout=10,
            read_timeout=15,
            retries={"total_max_attempts": 1, "mode": "standard"},
        ),
    )
    try:
        response = client.list_objects_v2(
            Bucket=config["bucket"],
            Prefix=config.get("base_prefix", "") or "",
            MaxKeys=_SAMPLE_LIMIT,
        )
        return max(0, min(int(response.get("KeyCount", 0)), _SAMPLE_LIMIT))
    finally:
        _close_registered(client)


def _probe_sftp(
    config: dict[str, Any],
    secret: dict[str, Any],
    known_hosts_file: str | None,
) -> int:
    import paramiko

    from .sftp import load_sftp_private_key

    client = paramiko.SSHClient()
    sftp = None
    entries = None
    try:
        with _close_on_deadline(client.close):
            if known_hosts_file:
                client.load_host_keys(known_hosts_file)
            else:
                client.load_system_host_keys()
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            private_key = load_sftp_private_key(secret)
            client.connect(
                hostname=config["host"],
                port=int(config.get("port", 22)),
                username=config["username"],
                password=secret.get("password") if not private_key else None,
                pkey=private_key,
                timeout=PROBE_CONNECT_TIMEOUT,
                auth_timeout=PROBE_CONNECT_TIMEOUT,
                channel_timeout=PROBE_CONNECT_TIMEOUT,
                allow_agent=False,
                look_for_keys=False,
            )
            sftp = client.open_sftp()
            channel = getattr(sftp, "get_channel", lambda: None)()
            if channel is not None:
                with suppress(Exception):
                    channel.settimeout(PROBE_CONNECT_TIMEOUT)
            entries = sftp.listdir_iter(
                config.get("base_path", ".") or ".", read_aheads=1
            )
            count = 0
            for _ in entries:
                count += 1
                if count >= _SAMPLE_LIMIT:
                    break
            return count
    finally:
        _close_all(entries, sftp, client)


__all__ = ["probe_connection"]
