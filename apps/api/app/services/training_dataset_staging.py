"""Stage training data on disk and preserve physical cleanup failures."""

import tempfile
from contextlib import contextmanager

from app.services import storage_access


@contextmanager
def temporary_directory(**kwargs):
    temporary = tempfile.TemporaryDirectory(**kwargs)
    try:
        yield temporary.name
    finally:
        try:
            temporary.cleanup()
        except BaseException as exc:
            raise storage_access.mark_unsettled(exc)
