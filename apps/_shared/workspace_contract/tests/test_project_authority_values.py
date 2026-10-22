import json
from uuid import uuid4

import pytest

from aap_workspace_contract.project_query_contract import PROJECT_ROW_COLUMNS
from aap_workspace_contract.project_query_values import (
    decode_project_authority_row,
)
from aap_workspace_contract.project_query_contract import ProjectQueryUnavailable


def _row(project_id, owner_id):
    value = {column: None for column in PROJECT_ROW_COLUMNS}
    value["id"] = str(project_id)
    value["owner_id"] = str(owner_id) if owner_id is not None else None
    return value


def test_project_authority_decoder_ignores_unrelated_project_values():
    project_id, owner_id = uuid4(), uuid4()
    value = _row(project_id, owner_id)
    value.update(
        tool_bindings={"legacy": ["untyped"]},
        due_date={"legacy": "date"},
        created_at=10000,
        updated_at=False,
    )

    assert decode_project_authority_row(json.dumps(value), target_id=project_id) == (
        project_id,
        owner_id,
    )


def test_project_authority_decoder_preserves_missing_project_and_nullable_owner():
    project_id = uuid4()

    assert decode_project_authority_row(None, target_id=project_id) is None
    assert decode_project_authority_row(
        json.dumps(_row(project_id, None)), target_id=project_id
    ) == (project_id, None)


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        json.dumps({"id": str(uuid4()), "owner_id": str(uuid4())}),
        json.dumps(_row(uuid4(), uuid4()) | {"unexpected": True}),
        json.dumps(_row("invalid", uuid4())),
        json.dumps(_row(uuid4(), "invalid")),
        '{"id":"00000000-0000-0000-0000-000000000001",'
        '"id":"00000000-0000-0000-0000-000000000001"}',
    ],
    ids=[
        "malformed-json",
        "wrong-shape",
        "extra-column",
        "bad-project-id",
        "bad-owner-id",
        "duplicate-key",
    ],
)
def test_project_authority_decoder_rejects_invalid_identity_or_row_shape(payload):
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_authority_row(payload, target_id=uuid4())


def test_project_authority_decoder_rejects_a_different_requested_project():
    with pytest.raises(ProjectQueryUnavailable):
        decode_project_authority_row(
            json.dumps(_row(uuid4(), uuid4())), target_id=uuid4()
        )
