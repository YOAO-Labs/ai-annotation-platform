"""Broker-only settings for the private Jobs queue owner."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping

_MAX_SECRET_BYTES = 4096


def _selector(env: Mapping[str, str], name: str) -> str:
    direct = env.get(name, "")
    file_path = env.get(f"{name}_FILE", "")
    if direct and file_path:
        raise ValueError("Jobs queue credential selectors conflict")
    if file_path:
        try:
            with Path(file_path).open("rb") as source:
                raw = source.read(_MAX_SECRET_BYTES + 1)
            if len(raw) > _MAX_SECRET_BYTES:
                raise ValueError("Jobs queue credential is invalid")
            value = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError("Jobs queue credential is invalid") from None
    else:
        value = direct
    if not value:
        raise ValueError("Jobs queue credential is missing")
    return value


@dataclass(frozen=True, slots=True)
class JobsQueueSettings:
    broker_url: str = field(repr=False)
    token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8190

    def __post_init__(self) -> None:
        if not self.broker_url or not self.token or not self.host:
            raise ValueError("Jobs queue settings are invalid")
        try:
            token_bytes = self.token.encode("ascii")
        except UnicodeEncodeError:
            raise ValueError("Jobs queue settings are invalid") from None
        if not token_bytes or any(value < 33 or value > 126 for value in token_bytes):
            raise ValueError("Jobs queue settings are invalid")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("Jobs queue settings are invalid")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> JobsQueueSettings:
        values = os.environ if env is None else env
        return cls(
            broker_url=_selector(values, "JOBS_QUEUE_BROKER_URL"),
            token=_selector(values, "JOBS_QUEUE_TOKEN"),
            host=values.get("JOBS_QUEUE_HOST", "127.0.0.1"),
            port=int(values.get("JOBS_QUEUE_PORT", "8190")),
        )
