"""Personal views persist queries, never membership or resource authorization."""

import uuid

from sqlalchemy import delete

from app.db.models.dataset import Dataset, DatasetItem
from app.db.models.dataset_saved_view import DatasetSavedView
from app.db.enums import UserRole
from tests.test_dataset_content import content_world as content_world, headers
from tests.test_dataset_source_changes import accept_all_files


async def dataset(db, user):
    row = Dataset(
        display_id=f"VIEW-{uuid.uuid4().hex[:8]}", name="Views", created_by=user.id
    )
    db.add(row)
    await db.commit()
    return row


async def test_private_view_crud_parent_ownership_and_validation(
    db_session, super_admin, annotator, httpx_client
):
    db, client = db_session, httpx_client
    ds = await dataset(db, super_admin[0])
    other = await dataset(db, super_admin[0])
    url = f"/api/v1/datasets/{ds.id}/saved-views"
    auth, admin = headers(annotator), headers(super_admin)
    payload = {
        "name": " My pictures ",
        "query": {"extension": ".JPEG", "min_width": 10},
    }
    created = await client.post(url, json=payload, headers=auth)
    assert created.status_code == 201, created.text
    view = created.json()
    assert view["name"] == "My pictures" and view["query"]["extension"] == "jpg"
    assert "owner_id" not in view
    assert (await client.get(url, headers=auth)).json()["items"][0]["id"] == view["id"]
    assert (await client.get(url, headers=admin)).json()["items"] == []
    assert (
        await client.put(url + "/" + view["id"], json=payload, headers=admin)
    ).status_code == 404
    assert (
        await client.delete(url + "/" + view["id"], headers=admin)
    ).status_code == 404
    wrong = f"/api/v1/datasets/{other.id}/saved-views/{view['id']}"
    assert (await client.put(wrong, json=payload, headers=auth)).status_code == 404
    assert (await client.delete(wrong, headers=auth)).status_code == 404
    assert (await client.post(url, json=payload, headers=auth)).status_code == 409
    for query in (
        {"file_path": "/etc"},
        {"min_width": 50, "max_width": 10},
        {"dimension_state": "unknown", "min_height": 1},
    ):
        assert (
            await client.post(url, json={"name": "bad", "query": query}, headers=auth)
        ).status_code == 422
    assert (
        await client.post(url, json={"name": "   "}, headers=auth)
    ).status_code == 422
    assert (
        await client.post(
            url, json={"name": "future", "query_schema_version": 2}, headers=auth
        )
    ).status_code == 422
    updated = await client.put(
        url + "/" + view["id"],
        json={"name": "Renamed", "query": {"media_type": "image"}},
        headers=auth,
    )
    assert updated.status_code == 200 and updated.json()["name"] == "Renamed"
    assert (
        await client.delete(url + "/" + view["id"], headers=auth)
    ).status_code == 204
    assert (await client.get(url, headers=auth)).json()["items"] == []


async def test_dynamic_membership_requeries_items(
    db_session, super_admin, httpx_client
):
    db, client = db_session, httpx_client
    ds = await dataset(db, super_admin[0])
    url = f"/api/v1/datasets/{ds.id}"
    auth = headers(super_admin)
    view = (
        await client.post(
            url + "/saved-views",
            json={"name": "PNG", "query": {"extension": "png", "min_width": 20}},
            headers=auth,
        )
    ).json()
    query = {k: v for k, v in view["query"].items() if v is not None}
    assert (await client.get(url + "/items", params=query, headers=auth)).json()[
        "total"
    ] == 0
    db.add(
        DatasetItem(
            dataset_id=ds.id,
            file_name="new.PNG",
            file_path="opaque-reference",
            width=30,
            height=40,
        )
    )
    await db.commit()
    page = await client.get(url + "/items", params=query, headers=auth)
    assert page.status_code == 200, page.text
    assert page.json()["total"] == 1
    assert (await client.get(url + "/saved-views", headers=auth)).json()["items"][0][
        "query"
    ] == view["query"]
    await db.execute(delete(Dataset).where(Dataset.id == ds.id))
    await db.commit()
    assert (await client.get(url + "/saved-views", headers=auth)).status_code == 404


async def test_source_conditions_recheck_authority_without_dataset_write_guard(
    db_session, content_world, super_admin, annotator, httpx_client
):
    db, client, world = db_session, httpx_client, content_world
    await accept_all_files(db, world)
    url = f"/api/v1/datasets/{world[3].id}"
    auth = headers(super_admin)
    payload = {"name": "Source", "query": {"source_id": str(world[5].id)}}
    view = await client.post(url + "/saved-views", json=payload, headers=auth)
    assert view.status_code == 201, view.text
    # An employee can save personal browsing preferences on a directory dataset;
    # this grants no write capability and no authority to select its source.
    employee = headers(annotator)
    assert (
        await client.post(url + "/saved-views", json={"name": "Mine"}, headers=employee)
    ).status_code == 201
    assert (
        await client.post(url + "/saved-views", json=payload, headers=employee)
    ).status_code == 404
    assert (
        await client.get(url + "/items", params=payload["query"], headers=employee)
    ).status_code == 404
    world[5].dataset_id = (await dataset(db, super_admin[0])).id
    await db.commit()
    assert (
        await client.get(url + "/items", params=payload["query"], headers=auth)
    ).status_code == 404
    assert (
        await client.put(
            url + "/saved-views/" + view.json()["id"], json=payload, headers=auth
        )
    ).status_code == 404
    # Deleting an invalid view must remain possible without selecting that source.
    assert (
        await client.delete(url + "/saved-views/" + view.json()["id"], headers=auth)
    ).status_code == 204


async def test_revoked_source_authority_and_bounded_private_views(
    db_session, content_world, super_admin, httpx_client
):
    db, client, world = db_session, httpx_client, content_world
    url, auth = f"/api/v1/datasets/{world[3].id}", headers(super_admin)
    payload = {"name": "Source", "query": {"source_id": str(world[5].id)}}
    view = await client.post(url + "/saved-views", json=payload, headers=auth)
    assert view.status_code == 201, view.text
    super_admin[0].role = UserRole.EMPLOYEE
    await db.commit()
    assert (
        await client.get(url + "/items", params=payload["query"], headers=auth)
    ).status_code == 404
    assert (
        await client.put(
            url + "/saved-views/" + view.json()["id"], json=payload, headers=auth
        )
    ).status_code == 404
    for index in range(99):
        db.add(
            DatasetSavedView(
                dataset_id=world[3].id, owner_id=super_admin[0].id, name=f"View {index}"
            )
        )
    await db.commit()
    assert (
        len((await client.get(url + "/saved-views", headers=auth)).json()["items"])
        == 100
    )
    assert (
        await client.post(url + "/saved-views", json={"name": "overflow"}, headers=auth)
    ).status_code == 409
    assert (
        await client.delete(url + "/saved-views/" + view.json()["id"], headers=auth)
    ).status_code == 204
