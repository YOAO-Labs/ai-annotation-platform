"""Compatibility imports for the Identity-owned password policy."""

from aap_identity.password_policy import MIN_LENGTH as MIN_LENGTH
from aap_identity.password_policy import (
    validate_password_strength as validate_password_strength,
)
