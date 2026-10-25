from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest

from aap_inference.pipeline_contract import PipelineActor, PipelineRejected, PipelineRow
from aap_inference.pipeline_policy import (
    assert_can_create_pipeline_scope,
    assert_pipeline_visible,
    can_edit_pipeline,
    validate_pipeline_scope_owner,
)


def _row(*, scope="private", created_by=None, project_id=None, organization_id=None):
    now = datetime.now(UTC)
    return PipelineRow(
        id=uuid.uuid4(),
        scope=scope,
        project_id=project_id,
        organization_id=organization_id,
        name="Example",
        stages=[],
        is_default=False,
        created_by=created_by or uuid.uuid4(),
        usage_count=0,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize(
    ("scope", "project_id", "organization_id", "message"),
    [
        (
            "private",
            None,
            None,
            "private 编排必须指定 project_id 且不能指定 organization_id",
        ),
        (
            "organization",
            uuid.uuid4(),
            uuid.uuid4(),
            "organization 编排必须指定 organization_id 且不能指定 project_id",
        ),
        (
            "public",
            None,
            uuid.uuid4(),
            "public 编排不能指定 project_id / organization_id",
        ),
    ],
)
def test_scope_owner_validation_keeps_legacy_messages(
    scope, project_id, organization_id, message
):
    with pytest.raises(ValueError) as error:
        validate_pipeline_scope_owner(scope, project_id, organization_id)
    assert str(error.value) == message


@pytest.mark.asyncio
async def test_super_admin_organization_creation_skips_membership_read():
    actor = PipelineActor(id=uuid.uuid4(), role="super_admin")
    organization_id = uuid.uuid4()
    reads = 0

    async def organization_ids():
        nonlocal reads
        reads += 1
        return []

    await assert_can_create_pipeline_scope(
        "organization", organization_id, actor, organization_ids
    )
    assert reads == 0


@pytest.mark.asyncio
async def test_organization_visibility_and_editability_keep_two_identity_reads():
    actor = PipelineActor(id=uuid.uuid4(), role="project_admin")
    organization_id = uuid.uuid4()
    row = _row(scope="organization", organization_id=organization_id)
    reads = 0

    async def organization_ids():
        nonlocal reads
        reads += 1
        return [organization_id]

    await assert_pipeline_visible(row, actor, organization_ids)
    assert await can_edit_pipeline(row, actor, organization_ids)
    assert reads == 2


@pytest.mark.asyncio
async def test_private_visibility_hides_non_creator_without_org_lookup():
    actor = PipelineActor(id=uuid.uuid4(), role="employee")
    row = _row(scope="private")
    reads = 0

    async def organization_ids():
        nonlocal reads
        reads += 1
        return []

    with pytest.raises(PipelineRejected) as rejected:
        await assert_pipeline_visible(row, actor, organization_ids)
    assert rejected.value.status_code == 404
    assert rejected.value.detail == "编排不存在"
    assert reads == 0
