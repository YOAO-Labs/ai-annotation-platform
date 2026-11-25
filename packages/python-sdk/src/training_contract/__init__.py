"""Versioned, offline training contracts shared by API and native executor."""

from .catalog import ADAPTERS, definition, definitions, schema_identity
from .validation import resolve, verify_frozen, verify_native

__all__ = [
    "ADAPTERS",
    "definition",
    "definitions",
    "schema_identity",
    "resolve",
    "verify_frozen",
    "verify_native",
]
