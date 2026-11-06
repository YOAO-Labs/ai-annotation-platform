"""Bind physical export I/O to the current independently owned execution."""

from contextvars import ContextVar
from pathlib import Path

from sqlalchemy.engine import make_url


def database_url(settings):
    direct = settings.export_execution_database_url
    file_path = settings.export_execution_database_url_file
    if direct and file_path:
        raise ValueError("Export execution database selectors conflict")
    if file_path:
        try:
            with Path(file_path).open("rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError("Export execution database credential is invalid")
            value = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            raise ValueError(
                "Export execution database credential is invalid"
            ) from None
    else:
        value = direct
    try:
        url = make_url(value)
    except Exception:
        raise ValueError("Export execution database URL is invalid") from None
    if url.get_backend_name() != "postgresql" or not url.host:
        raise ValueError("Export execution database URL is invalid")
    return url.set(drivername="postgresql+asyncpg")


_current = ContextVar("export_execution", default=None)


def bind(execution):
    return _current.set(execution)


def reset(token):
    _current.reset(token)


def current():
    execution = _current.get()
    if execution is None:
        raise RuntimeError("Export work has no execution owner")
    return execution


async def enter_io():
    await current().enter_io()
