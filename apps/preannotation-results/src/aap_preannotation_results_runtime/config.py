"""Scoped database URL configuration with bounded secret-file support."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

MAX_DATABASE_URL_BYTES = 4096
DATABASE_URL_ENV = "PREANNOTATION_RESULTS_DATABASE_URL"


class ConfigurationError(RuntimeError):
    """A safe configuration failure without secret values."""


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    database_url: str = field(repr=False)


def _read_database_url(values: Mapping[str, str]) -> str:
    inline = values.get(DATABASE_URL_ENV)
    secret_file = values.get(f"{DATABASE_URL_ENV}_FILE")
    if inline and secret_file:
        raise ConfigurationError(f"set only one of {DATABASE_URL_ENV} and its file")
    if secret_file:
        try:
            with Path(secret_file).open("rb") as handle:
                raw = handle.read(MAX_DATABASE_URL_BYTES + 1)
        except OSError:
            raise ConfigurationError(
                "unable to read the preannotation database URL file"
            ) from None
        if len(raw) > MAX_DATABASE_URL_BYTES:
            raise ConfigurationError(
                "preannotation database URL file exceeds its size limit"
            )
        try:
            inline = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ConfigurationError(
                "preannotation database URL must be UTF-8"
            ) from None
        if inline.endswith("\r\n"):
            inline = inline[:-2]
        elif inline.endswith("\n"):
            inline = inline[:-1]
    value = inline or ""
    if not value or len(value.encode("utf-8")) > MAX_DATABASE_URL_BYTES:
        raise ConfigurationError(f"missing or oversized {DATABASE_URL_ENV}")
    try:
        parsed = urlsplit(value)
        parsed.port
        valid = parsed.scheme in {"postgres", "postgresql"} and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f"{DATABASE_URL_ENV} must be a PostgreSQL URL")
    return value


def load_config(environment: Mapping[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if environment is None else environment
    return RuntimeConfig(database_url=_read_database_url(values))
