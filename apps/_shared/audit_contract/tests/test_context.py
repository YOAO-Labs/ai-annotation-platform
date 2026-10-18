import pytest
from pydantic import ValidationError

from aap_audit_contract import ProjectDeletionAuditContext


def test_context_keeps_its_frozen_schema_name_and_fields() -> None:
    schema = ProjectDeletionAuditContext.model_json_schema()

    assert schema["title"] == "ProjectDeletionAuditContext"
    assert set(schema["properties"]) == {"method", "path", "ip", "request_id"}
    assert schema["additionalProperties"] is False

    value = ProjectDeletionAuditContext(method="DELETE", path="/projects/1")
    assert value.model_dump() == {
        "method": "DELETE",
        "path": "/projects/1",
        "ip": None,
        "request_id": None,
    }


def test_context_rejects_extra_or_oversized_values() -> None:
    with pytest.raises(ValidationError):
        ProjectDeletionAuditContext(untrusted="value")
    with pytest.raises(ValidationError):
        ProjectDeletionAuditContext(method="D" * 9)
