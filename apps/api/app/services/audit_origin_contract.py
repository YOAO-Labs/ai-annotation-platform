"""Persistence-free audit origin facts passed from adapters to workflows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AuditOriginContext:
    method: str | None
    path: str | None
    ip: str | None
