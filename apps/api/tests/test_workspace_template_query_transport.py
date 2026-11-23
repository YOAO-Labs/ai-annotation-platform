"""The public template reader borrows the caller's transaction and actor."""

import uuid

import pytest

from app.db.models.project_template import ProjectTemplate


@pytest.mark.asyncio
async def test_pending_private_template_is_visible_to_owner_only_and_rolls_back(
    db_session, httpx_client, project_admin, annotator
):
    owner, owner_token = project_admin
    _, outsider_token = annotator
    template_id = uuid.uuid4()
    template = ProjectTemplate(
        id=template_id,
        display_id=f"T-M2B-{template_id.hex[:12]}",
        name="Pending private template",
        type_label="Image",
        type_key="image-det",
        scope="private",
        created_by=owner.id,
    )
    path = "/api/v1/project-templates"
    target = f"{path}/{template_id}"
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    async with db_session.begin_nested() as savepoint:
        db_session.add(template)
        owner_list = await httpx_client.get(
            path,
            params={"scope": "private", "search": template.name},
            headers=owner_headers,
        )
        assert owner_list.status_code == 200, owner_list.text
        assert [row["id"] for row in owner_list.json()] == [str(template_id)]
        owner_detail = await httpx_client.get(target, headers=owner_headers)
        assert owner_detail.status_code == 200, owner_detail.text
        assert owner_detail.json() == owner_list.json()[0]
        hidden = await httpx_client.get(target, headers=outsider_headers)
        assert hidden.status_code == 404
        assert hidden.json() == {"detail": "模板不存在"}
        await savepoint.rollback()

    rolled_back = await httpx_client.get(target, headers=owner_headers)
    assert rolled_back.status_code == 404
    assert rolled_back.json() == {"detail": "模板不存在"}
