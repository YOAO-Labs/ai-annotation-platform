"""Portable password-strength policy shared by account and API adapters."""

from __future__ import annotations

import re

MIN_LENGTH = 8


def validate_password_strength(password: str) -> list[str]:
    errors: list[str] = []
    if len(password) < MIN_LENGTH:
        errors.append(f"密码长度至少 {MIN_LENGTH} 位")
    if not re.search(r"[A-Z]", password):
        errors.append("密码需包含大写字母")
    if not re.search(r"[a-z]", password):
        errors.append("密码需包含小写字母")
    if not re.search(r"\d", password):
        errors.append("密码需包含数字")
    return errors
