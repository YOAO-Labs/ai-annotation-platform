from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from aap_audit.read_relation_contract import AuditReadEvent


def _event(**overrides) -> dict:
    values = {
        "id": 17,
        "actor_id": "00000000-0000-4000-8000-000000000320",
        "actor_email": "actor@example.test",
        "actor_role": "project_admin",
        "action": "task.submit",
        "target_type": "task",
        "target_id": "task-17",
        "method": "POST",
        "path": "/api/v1/tasks/task-17/submit",
        "status_code": 200,
        "ip": ipaddress.ip_address("127.0.0.1"),
        "detail_json": {"phase": "submitted", "round": [1, None, True]},
        "request_id": "request-17",
        "created_at": datetime(2026, 10, 24, 12, 34, 56, tzinfo=timezone.utc),
    }
    return {**values, **overrides}


@pytest.mark.parametrize(
    "value",
    [
        ipaddress.ip_address("127.0.0.1"),
        ipaddress.ip_address("2001:db8::1"),
        ipaddress.ip_interface("192.0.2.13/24"),
        ipaddress.ip_interface("2001:db8::2/64"),
        ipaddress.ip_address("::ffff:192.0.2.1"),
        ipaddress.ip_interface("::ffff:192.0.2.1/120"),
    ],
)
def test_row_contract_keeps_bigint_json_and_native_inet_text_lossless(value):
    row = AuditReadEvent.model_validate(
        _event(
            id=-9223372036854775808,
            ip=value,
            detail_json=None,
        )
    )
    assert row.id == "-9223372036854775808"
    assert row.ip == str(value)
    assert row.detail_json is None


def test_row_contract_rejects_noncanonical_or_out_of_range_bigint_and_naive_time():
    for invalid_id in ("-0", "01", "9223372036854775808", "-9223372036854775809"):
        with pytest.raises(ValidationError):
            AuditReadEvent.model_validate(_event(id=invalid_id))

    with pytest.raises(ValidationError):
        AuditReadEvent.model_validate(
            _event(created_at=datetime(2026, 10, 24, 12, 34, 56))
        )


def test_package_root_contract_import_does_not_require_optional_sqlalchemy():
    source = Path(__file__).resolve().parents[1] / "src"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source), environment.get("PYTHONPATH", "")]
    )
    script = """
import importlib.abc
import sys
class BlockSQLAlchemy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'sqlalchemy' or fullname.startswith('sqlalchemy.'):
            raise ModuleNotFoundError('blocked optional dependency', name=fullname)
sys.meta_path.insert(0, BlockSQLAlchemy())
import aap_audit
assert aap_audit.READ_RELATION_PROTOCOL == 'aap-audit-read-relations/v1'
assert aap_audit.AuditReadEvent is not None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
