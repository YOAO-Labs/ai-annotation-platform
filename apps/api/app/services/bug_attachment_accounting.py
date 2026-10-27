"""Compatibility adapter for bug attachment conditional-write accounting."""

from app.services import conditional_write_accounting as shared
from app.services.conditional_write_accounting import (
    Observation as Observation,
    finish as finish,
    immutable as immutable,
)


def recorder(db, receipt):
    return shared.recorder(
        db, receipt, producer="bug_attachment_uploads", purpose="other_platform"
    )
