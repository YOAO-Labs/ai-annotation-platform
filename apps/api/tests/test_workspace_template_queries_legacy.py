"""Capture the live legacy template reads and response projections."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

import httpx
import pytest
from sqlalchemy import delete, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.security import create_access_token
from app.db.models.organization import Organization, OrganizationMember
from app.db.models.project import Project
from app.db.models.project_template import ProjectTemplate
from app.db.models.user import User
from app.services.identity_contracts import IdentityActorFacts
from app.services import workspace_configuration
from app.api.v1.project_templates import get_template, list_templates
from tests.factory import build_tool_bindings, create_user

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1"
        or os.environ.get("AAP_WORKSPACE_TEMPLATE_QUERY_CAPTURE") != "1",
        reason="requires the explicit owned legacy template-query capture",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]
_BASE_COMMIT = "a0317013841ca3aaf1b53b06ce051c6eb07ac798"
_CAPTURE_PATH = _ROOT / ".worktree/workspace-template-query-legacy-samples.json"
_ROUTE_PATH = "/api/v1/project-templates"
_BASE_TIME = datetime(2020, 1, 1, tzinfo=timezone.utc)
_TIMESTAMP_FIELDS = {"created_at", "updated_at"}
_FIXED_TIMESTAMP_PREFIX = _BASE_TIME.isoformat()[:10]
_RELEVANT_HEADERS = ("content-type", "location", "www-authenticate", "retry-after")
_SOURCE_FILES = (
    "apps/api/app/api/v1/project_templates.py",
    "apps/api/app/services/project_template.py",
    "apps/api/app/services/workspace_configuration.py",
    "apps/api/app/db/models/project_template.py",
    "apps/workspace/src/aap_workspace/configuration_requests.py",
    "apps/workspace/src/aap_workspace/configuration_policy.py",
)
_MISSING = object()


def _legacy_runtime_url(test_engine) -> str:
    value = os.environ.get("AAP_MODULE_DATA_LEGACY_RUNTIME_URL")
    assert value and os.environ.get("AAP_MODULE_DATA_PG_CONTAINER_ID")
    url = make_url(value)
    assert url.username == "legacy_runtime"
    assert url.database and url.database.endswith("_test")
    assert (url.host, url.port, url.database) == (
        test_engine.url.host,
        test_engine.url.port,
        test_engine.url.database,
    )
    return url.set(drivername="postgresql+asyncpg").render_as_string(
        hide_password=False
    )


def _source_metadata() -> dict:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for relative in _SOURCE_FILES:
        committed = subprocess.run(
            ["git", "show", f"{_BASE_COMMIT}:{relative}"],
            cwd=_ROOT,
            check=True,
            capture_output=True,
        ).stdout
        assert committed == (_ROOT / relative).read_bytes(), relative
    return {
        "captured_head": head,
        "pre_port_base": _BASE_COMMIT,
        "capture_driver_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime_source_sha256": {
            relative: hashlib.sha256((_ROOT / relative).read_bytes()).hexdigest()
            for relative in _SOURCE_FILES
        },
    }


def _normalise(value, aliases: dict[str, str], *, key: str | None = None):
    if key in _TIMESTAMP_FIELDS and value is not None:
        if isinstance(value, str) and value.startswith(_FIXED_TIMESTAMP_PREFIX):
            return value
        return "<timestamp>"
    if isinstance(value, uuid.UUID):
        value = str(value)
    if isinstance(value, str):
        if value in aliases:
            return aliases[value]
        for actual, alias in aliases.items():
            value = value.replace(actual, alias)
        return value
    if isinstance(value, (list, tuple)):
        return [_normalise(item, aliases) for item in value]
    if isinstance(value, dict):
        return {
            name: _normalise(item, aliases, key=name) for name, item in value.items()
        }
    return value


async def _request(
    client: httpx.AsyncClient,
    capture: dict,
    aliases: dict[str, str],
    actors: dict[str, User],
    tokens: dict[str, str],
    *,
    actor_alias: str,
    method: str,
    path: str,
    params=None,
    body=None,
    active: bool = True,
    new_resource_alias: str | None = None,
):
    kwargs = {"headers": {"Authorization": f"Bearer {tokens[actor_alias]}"}}
    if params is not None:
        kwargs["params"] = params
    if body is not None:
        kwargs["json"] = body
    response = await client.request(method, path, **kwargs)
    try:
        raw_body = response.json() if response.content else None
    except ValueError:
        raw_body = response.text
    if new_resource_alias and isinstance(raw_body, dict) and raw_body.get("id"):
        aliases[str(raw_body["id"])] = f"${new_resource_alias}"
        if raw_body.get("display_id"):
            aliases[str(raw_body["display_id"])] = f"${new_resource_alias}_display_id"
    actor = actors[actor_alias]
    headers = {
        name: response.headers[name]
        for name in _RELEVANT_HEADERS
        if name in response.headers
    }
    capture["samples"].append(
        {
            "request": {
                "method": method.upper(),
                "path": _normalise(path, aliases),
                "params": _normalise(params, aliases),
                "body": _normalise(body, aliases),
                "actor": {
                    "alias": actor_alias,
                    "id": aliases[str(actor.id)],
                    "role": actor.role,
                    "active": active,
                },
            },
            "response": {
                "status": response.status_code,
                "headers": headers,
                "body": _normalise(raw_body, aliases),
            },
        }
    )
    return response, raw_body


def _seed_template(
    db,
    aliases: dict[str, str],
    *,
    alias: str,
    created_by: User,
    order: int,
    name: str,
    scope: str,
    type_key: str = "image-det",
    organization_id: uuid.UUID | None = None,
    **fields,
) -> ProjectTemplate:
    display_id = f"PT-{uuid.uuid4().hex[:8]}"
    template = ProjectTemplate(
        id=uuid.uuid4(),
        display_id=display_id,
        name=name,
        type_label="Image label",
        type_key=type_key,
        data_type="video" if type_key.startswith("video") else "image",
        scope=scope,
        organization_id=organization_id,
        created_by=created_by.id,
        created_at=_BASE_TIME + timedelta(minutes=order),
        updated_at=_BASE_TIME + timedelta(minutes=order),
        **fields,
    )
    db.add(template)
    aliases[str(template.id)] = f"${alias}"
    aliases[display_id] = f"${alias}_display_id"
    return template


async def _direct_read_probe(
    *,
    admin_maker,
    runtime_maker,
    actor: User,
    no_autoflush: bool,
) -> dict:
    pending_id = uuid.uuid4()
    pending = ProjectTemplate(
        id=pending_id,
        display_id=f"PT-{uuid.uuid4().hex[:8]}",
        name=f"Uncommitted read probe {pending_id.hex[:8]}",
        type_label="Image label",
        type_key="image-det",
        created_by=actor.id,
        scope="private",
        created_at=_BASE_TIME + timedelta(days=30),
        updated_at=_BASE_TIME + timedelta(days=30),
    )
    facts = IdentityActorFacts(
        id=actor.id,
        name=actor.name,
        email=actor.email,
        role=actor.role,
        is_active=True,
    )
    async with runtime_maker() as db:
        db.add(pending)
        if no_autoflush:
            with db.no_autoflush:
                rows = await list_templates(
                    scope="private", type_key=None, search=None, db=db, user=facts
                )
        else:
            rows = await list_templates(
                scope="private", type_key=None, search=None, db=db, user=facts
            )
        included = any(row["id"] == pending_id for row in rows)
        remains_unflushed = pending in db.new
        caller_transaction_open = db.in_transaction()
        async with admin_maker() as observer:
            observer_visible = (
                await observer.get(ProjectTemplate, pending_id) is not None
            )
        await db.rollback()
    return {
        "no_autoflush": no_autoflush,
        "included_in_route_result": included,
        "pending_in_session_new_after_read": remains_unflushed,
        "caller_transaction_open_after_read": caller_transaction_open,
        "visible_from_owner_before_caller_rollback": observer_visible,
    }


async def _timezone_read_probe(runtime_maker, actor: User, template_id) -> dict:
    facts = IdentityActorFacts(
        id=actor.id,
        name=actor.name,
        email=actor.email,
        role=actor.role,
        is_active=True,
    )
    async with runtime_maker() as db:
        before_set = await db.scalar(text("SELECT current_setting('TimeZone')"))
        await db.execute(text("SET LOCAL TIME ZONE 'Asia/Shanghai'"))
        before_read = await db.scalar(text("SELECT current_setting('TimeZone')"))
        rows = await list_templates(
            scope=None, type_key=None, search=None, db=db, user=facts
        )
        after_list = await db.scalar(text("SELECT current_setting('TimeZone')"))
        list_created_at = next(
            row["created_at"] for row in rows if row["id"] == template_id
        )
        detail = await get_template(template_id, db=db, user=facts)
        after_detail = await db.scalar(text("SELECT current_setting('TimeZone')"))
        detail_created_at = detail["created_at"]
        await db.rollback()
    return {
        "timezone_before_set": before_set,
        "timezone_before_reads": before_read,
        "timezone_after_list": after_list,
        "timezone_after_detail": after_detail,
        "list_created_at_iso": list_created_at.isoformat(),
        "detail_created_at_iso": detail_created_at.isoformat(),
    }


async def _cleanup_owned(
    admin_maker,
    *,
    actor_ids: list[uuid.UUID],
    organization_ids: list[uuid.UUID],
    membership_ids: list[uuid.UUID],
    operation_ids: list[uuid.UUID],
) -> None:
    async with admin_maker.begin() as db:
        for operation_id in operation_ids:
            params = {"operation_id": str(operation_id)}
            for table in (
                "workspace_configuration_operation_states",
                "workspace_configuration_operation_attempts",
                "workspace_configuration_operation_receipts",
            ):
                await db.execute(
                    text(
                        f"DELETE FROM public.{table} "
                        "WHERE operation_id=CAST(:operation_id AS uuid)"
                    ),
                    params,
                )
        if actor_ids:
            await db.execute(delete(Project).where(Project.owner_id.in_(actor_ids)))
            await db.execute(
                delete(ProjectTemplate).where(ProjectTemplate.created_by.in_(actor_ids))
            )
        if membership_ids:
            await db.execute(
                delete(OrganizationMember).where(
                    OrganizationMember.id.in_(membership_ids)
                )
            )
        if organization_ids:
            await db.execute(
                delete(Organization).where(Organization.id.in_(organization_ids))
            )
        if actor_ids:
            await db.execute(delete(User).where(User.id.in_(actor_ids)))


def _write_capture(capture: dict) -> None:
    _CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _CAPTURE_PATH.open("x", encoding="utf-8") as stream:
        json.dump(capture, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")


async def test_capture_legacy_template_query_and_projection_samples(
    test_engine, app_module, monkeypatch
):
    from app.config import settings
    from app.deps import get_db

    assert not _CAPTURE_PATH.exists(), f"refusing to overwrite {_CAPTURE_PATH}"
    runtime_url = _legacy_runtime_url(test_engine)
    admin_maker = async_sessionmaker(test_engine, expire_on_commit=False)
    runtime_engine = create_async_engine(runtime_url, hide_parameters=True)
    runtime_maker = async_sessionmaker(runtime_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex[:10]
    actor_ids: list[uuid.UUID] = []
    project_ids: list[uuid.UUID] = []
    organization_ids: list[uuid.UUID] = []
    membership_ids: list[uuid.UUID] = []
    operation_ids: list[uuid.UUID] = []
    aliases: dict[str, str] = {suffix: "$capture_suffix"}
    actors: dict[str, User] = {}
    tokens: dict[str, str] = {}
    template_by_alias: dict[str, ProjectTemplate] = {}
    capture = {
        "capture_kind": "actual-pre-port-template-query-and-response-projection",
        "source": _source_metadata(),
        "runtime_identity": None,
        "schema_boundaries": {
            "unknown_scope_row": "omitted: ck_project_template_scope rejects it",
            "organization_scope_without_id": (
                "omitted: ck_project_template_org_scope rejects it"
            ),
            "organization_id_null_policy": (
                "policy helper permits it, but the current database constraint prevents storage"
            ),
        },
        "samples": [],
        "direct_read_probes": [],
    }

    previous_db_override = app_module.dependency_overrides.get(get_db, _MISSING)
    original_configuration_request = workspace_configuration.configuration_request

    def capture_operation_id(**kwargs):
        request = original_configuration_request(**kwargs)
        operation_ids.append(request.operation_id)
        return request

    async def request_db():
        async with runtime_maker() as db:
            yield db

    monkeypatch.setattr(settings, "workspace_configuration_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url", "")
    monkeypatch.setattr(settings, "workspace_configuration_database_url_file", "")
    monkeypatch.setattr(settings, "audit_async", True)
    monkeypatch.setattr("app.middleware.audit._enqueue_audit", lambda _payload: True)
    monkeypatch.setattr(
        workspace_configuration, "configuration_request", capture_operation_id
    )
    app_module.dependency_overrides[get_db] = request_db

    try:
        async with runtime_engine.connect() as connection:
            role, database = (
                await connection.execute(
                    text("SELECT current_user, current_database()")
                )
            ).one()
        assert role == "legacy_runtime"
        assert database == test_engine.url.database
        capture["runtime_identity"] = {"database_role": role, "database": database}

        async with admin_maker.begin() as db:
            for alias, role, name in (
                ("super_admin", "super_admin", "Baseline Super Admin"),
                ("project_admin", "project_admin", "Baseline Project Admin"),
                ("employee", "employee", "Baseline Employee"),
                ("viewer", "viewer", "Baseline Viewer"),
                ("annotator", "annotator", "Historical Annotator"),
                ("reviewer", "reviewer", "Historical Reviewer"),
                ("unknown_role", "historical_observer", "Historical Unknown Role"),
                ("inactive_creator", "employee", "Inactive Creator"),
                ("other_creator", "employee", "Other Private Creator"),
                ("blank_creator", "employee", ""),
            ):
                user = await create_user(
                    db,
                    role,
                    f"template-query-{alias}-{suffix}@example.test",
                    name,
                )
                actors[alias] = user
                actor_ids.append(user.id)
                aliases[str(user.id)] = f"$actor_{alias}"
                tokens[alias] = create_access_token(subject=str(user.id), role=role)

            organizations = {
                alias: Organization(
                    id=uuid.uuid4(),
                    name=f"Template query {alias} organization {suffix}",
                    slug=f"template-query-{alias}-{suffix}",
                    created_by=actors["super_admin"].id,
                )
                for alias in ("member", "nonmember", "retired")
            }
            db.add_all(organizations.values())
            await db.flush()
            organization_ids.extend(org.id for org in organizations.values())
            aliases.update(
                {
                    str(org.id): f"$organization_{alias}"
                    for alias, org in organizations.items()
                }
            )
            membership = OrganizationMember(
                id=uuid.uuid4(),
                organization_id=organizations["member"].id,
                user_id=actors["project_admin"].id,
                role="member",
            )
            retired_membership = OrganizationMember(
                id=uuid.uuid4(),
                organization_id=organizations["retired"].id,
                user_id=actors["project_admin"].id,
                role="member",
                deleted_at=_BASE_TIME,
            )
            db.add_all([membership, retired_membership])
            membership_ids.extend([membership.id, retired_membership.id])

            bindings = build_tool_bindings(
                [
                    {"name": "car", "color": "#ff0000", "alias": "Vehicle"},
                    "person",
                ],
                attribute_schema={
                    "fields": [
                        {"key": "confidence", "label": "Confidence", "type": "number"}
                    ]
                },
            )
            template_by_alias["public_image"] = _seed_template(
                db,
                aliases,
                alias="template_public_image",
                created_by=actors["inactive_creator"],
                order=1,
                name="Public Mixed Case 42%",
                scope="public",
                tool_bindings=bindings,
                label_config={"nested": {"values": [1, {"unicode": "猫"}]}},
                rendering_config={
                    "smoothImage": True,
                    "cssImageFilter": "contrast(1.1)",
                    "box3dDefaultSize": [1, 2, 3],
                },
                annotation_guide="# Deep guide\n\n- keep nested data",
                usage_count=5,
                description="Deep JSON and computed-class projection",
            )
            template_specs = (
                ("public_video", "other_creator", "Public 视频 猫"),
                ("public_empty_creator", "blank_creator", "Public blank creator"),
                ("private_project_admin", "project_admin", "Own private admin"),
                ("private_employee", "employee", "Own private employee"),
                ("private_viewer", "viewer", "Own private viewer"),
                ("private_other", "other_creator", "Other private template"),
                ("organization_member", "other_creator", "Organization member"),
                ("organization_nonmember", "other_creator", "Organization nonmember"),
                (
                    "organization_retired_member",
                    "other_creator",
                    "Retired member organization",
                ),
                ("private_annotator", "annotator", "Own private annotator"),
                ("private_reviewer", "reviewer", "Own private reviewer"),
                ("private_unknown_role", "unknown_role", "Own private unknown"),
            )
            org_aliases = {
                "organization_member": "member",
                "organization_nonmember": "nonmember",
                "organization_retired_member": "retired",
            }
            for order, (alias, creator, name) in enumerate(template_specs, start=2):
                scope = (
                    "organization"
                    if alias.startswith("organization_")
                    else ("public" if alias.startswith("public_") else "private")
                )
                type_key = "video-track" if alias == "public_video" else "image-det"
                template_by_alias[alias] = _seed_template(
                    db,
                    aliases,
                    alias=f"template_{alias}",
                    created_by=actors[creator],
                    order=order,
                    name=name,
                    scope=scope,
                    type_key=type_key,
                    organization_id=(
                        organizations[org_aliases[alias]].id
                        if alias in org_aliases
                        else None
                    ),
                    **(
                        {"description": None, "annotation_guide": None}
                        if alias == "public_video"
                        else {}
                    ),
                )
            actors["inactive_creator"].is_active = False

        transport = httpx.ASGITransport(app=app_module, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", timeout=20
        ) as client:

            async def request(actor_alias, method, path, **kwargs):
                return await _request(
                    client,
                    capture,
                    aliases,
                    actors,
                    tokens,
                    actor_alias=actor_alias,
                    method=method,
                    path=path,
                    **kwargs,
                )

            list_records = {}
            for actor_alias in ("super_admin", "project_admin", "employee", "viewer"):
                response, body = await request(actor_alias, "GET", _ROUTE_PATH)
                assert response.status_code == 200, response.text
                list_records[actor_alias] = body

            def response_ids(records):
                return {row["id"] for row in records}

            public_ids = {
                str(template_by_alias[name].id)
                for name in ("public_image", "public_video", "public_empty_creator")
            }
            project_admin_ids = public_ids | {
                str(template_by_alias["private_project_admin"].id),
                str(template_by_alias["organization_member"].id),
            }
            employee_ids = public_ids | {str(template_by_alias["private_employee"].id)}
            viewer_ids = public_ids | {str(template_by_alias["private_viewer"].id)}
            super_admin_ids = {
                str(template.id) for template in template_by_alias.values()
            }
            assert response_ids(list_records["super_admin"]) == super_admin_ids
            assert response_ids(list_records["project_admin"]) == project_admin_ids
            assert response_ids(list_records["employee"]) == employee_ids
            assert response_ids(list_records["viewer"]) == viewer_ids
            expected_super_admin_order = [
                str(template.id)
                for template in sorted(
                    template_by_alias.values(),
                    key=lambda item: item.created_at,
                    reverse=True,
                )
            ]
            assert [row["id"] for row in list_records["super_admin"]] == (
                expected_super_admin_order
            )
            inactive_creator_projection = next(
                row
                for row in list_records["project_admin"]
                if row["id"] == str(template_by_alias["public_image"].id)
            )
            assert inactive_creator_projection["created_by_name"] == "Inactive Creator"
            assert inactive_creator_projection["rendering_config"][
                "box3dDefaultSize"
            ] == [
                1.0,
                2.0,
                3.0,
            ]
            blank_creator_projection = next(
                row
                for row in list_records["project_admin"]
                if row["id"] == str(template_by_alias["public_empty_creator"].id)
            )
            assert blank_creator_projection["created_by_name"] == ""

            query_cases = (
                ("scope_public", [("scope", "public")]),
                ("scope_private", [("scope", "private")]),
                ("scope_organization", [("scope", "organization")]),
                (
                    "repeated_type_keys",
                    [
                        ("type_key", "image-det"),
                        ("type_key", "video-track"),
                    ],
                ),
                ("empty_type_key", [("type_key", "")]),
                ("search_case_insensitive", [("search", "mixed case")]),
                ("search_percent_wildcard", [("search", "%")]),
                ("search_underscore_wildcard", [("search", "_")]),
                ("search_empty", [("search", "")]),
                ("search_unicode", [("search", "猫")]),
                ("search_no_match", [("search", "no-such-template")]),
                ("invalid_scope", [("scope", "historical")]),
            )
            query_results = {}
            for alias, params in query_cases:
                response, body = await request(
                    "project_admin", "GET", _ROUTE_PATH, params=params
                )
                if alias == "invalid_scope":
                    assert response.status_code == 422
                    query_results[alias] = response.status_code
                else:
                    assert response.status_code == 200, response.text
                    query_results[alias] = response_ids(body)
            assert query_results["scope_public"] == public_ids
            assert query_results["scope_private"] == {
                str(template_by_alias["private_project_admin"].id)
            }
            assert query_results["scope_organization"] == {
                str(template_by_alias["organization_member"].id)
            }
            assert query_results["repeated_type_keys"] == project_admin_ids
            assert query_results["empty_type_key"] == set()
            assert query_results["search_case_insensitive"] == {
                str(template_by_alias["public_image"].id)
            }
            assert query_results["search_unicode"] == {
                str(template_by_alias["public_video"].id)
            }
            assert query_results["search_no_match"] == set()
            assert query_results["search_empty"] == project_admin_ids
            assert query_results["search_percent_wildcard"] == project_admin_ids
            assert query_results["search_underscore_wildcard"] == project_admin_ids

            for actor_alias, template_alias in (
                ("annotator", "private_annotator"),
                ("reviewer", "private_reviewer"),
                ("unknown_role", "private_unknown_role"),
            ):
                await request(actor_alias, "GET", _ROUTE_PATH)
                await request(
                    actor_alias,
                    "GET",
                    f"{_ROUTE_PATH}/{template_by_alias[template_alias].id}",
                )

            detail_cases = (
                ("super_admin", "private_other", 200),
                ("super_admin", "organization_nonmember", 200),
                ("project_admin", "private_project_admin", 200),
                ("project_admin", "private_other", 404),
                ("project_admin", "organization_member", 200),
                ("project_admin", "organization_nonmember", 404),
                ("project_admin", "organization_retired_member", 404),
                ("employee", "private_project_admin", 404),
                ("viewer", "public_video", 200),
            )
            for actor_alias, template_alias, expected_status in detail_cases:
                template = template_by_alias[template_alias]
                response, _body = await request(
                    actor_alias, "GET", f"{_ROUTE_PATH}/{template.id}"
                )
                assert response.status_code == expected_status, response.text

            missing_template_id = uuid.uuid4()
            aliases[str(missing_template_id)] = "$missing_template"
            missing_response, _ = await request(
                "project_admin",
                "GET",
                f"{_ROUTE_PATH}/{missing_template_id}",
            )
            assert missing_response.status_code == 404

            create_body = {
                "name": f"Created projection {suffix}",
                "description": "Preserved create description",
                "type_label": "Image label",
                "type_key": "image-det",
                "tool_bindings": bindings,
                "label_config": {"nested": ["created", {"value": "猫"}]},
                "rendering_config": {"smoothImage": True},
                "annotation_guide": "# Created guide",
            }
            created_response, created_body = await request(
                "project_admin",
                "POST",
                _ROUTE_PATH,
                body=create_body,
                new_resource_alias="template_created",
            )
            assert created_response.status_code == 201, created_response.text
            created_template_id = uuid.UUID(created_body["id"])
            assert created_body["classes"] == ["car", "person"]
            assert created_body["classes_config"]["car"]["color"] == "#ff0000"
            assert created_body["attribute_schema"]["fields"][0]["key"] == "confidence"
            assert created_body["created_by_name"] == "Baseline Project Admin"
            assert created_body["label_config"] == create_body["label_config"]
            assert created_body["rendering_config"]["smoothImage"] is True

            update_body = {
                "name": f"Updated projection {suffix}",
                "annotation_guide": None,
                "tool_bindings": {},
            }
            updated_response, updated_body = await request(
                "project_admin",
                "PATCH",
                f"{_ROUTE_PATH}/{created_template_id}",
                body=update_body,
            )
            assert updated_response.status_code == 200, updated_response.text
            assert updated_body["description"] == create_body["description"]
            assert updated_body["annotation_guide"] is None
            assert updated_body["classes"] == []
            assert updated_body["tool_bindings"] == {}

            duplicate_response, duplicate_body = await request(
                "project_admin",
                "POST",
                f"{_ROUTE_PATH}/{template_by_alias['public_image'].id}/duplicate",
                new_resource_alias="template_duplicate",
            )
            assert duplicate_response.status_code == 201, duplicate_response.text
            assert duplicate_body["scope"] == "private"
            assert (
                duplicate_body["tool_bindings"]
                == inactive_creator_projection["tool_bindings"]
            )
            assert (
                duplicate_body["label_config"]
                == inactive_creator_projection["label_config"]
            )

            apply_response, apply_body = await request(
                "project_admin",
                "POST",
                path="/api/v1/projects",
                body={
                    "name": f"Applied projection template {suffix}",
                    "template_id": str(template_by_alias["public_image"].id),
                },
                new_resource_alias="project_applied",
            )
            assert apply_response.status_code == 200, apply_response.text
            project_ids.append(uuid.UUID(apply_body["id"]))

            usage_response, usage_body = await request(
                "project_admin",
                "GET",
                f"{_ROUTE_PATH}/{template_by_alias['public_image'].id}",
            )
            assert usage_response.status_code == 200, usage_response.text
            assert usage_body["usage_count"] == 6

            null_response, _null_body = await request(
                "project_admin",
                "PATCH",
                f"{_ROUTE_PATH}/{created_template_id}",
                body={"label_config": None},
            )
            assert null_response.status_code == 500
            async with admin_maker.begin() as db:
                stored = (
                    await db.execute(
                        text(
                            "SELECT label_config IS NULL, jsonb_typeof(label_config) "
                            "FROM public.project_templates WHERE id=:id"
                        ),
                        {"id": created_template_id},
                    )
                ).one()
                assert stored[0] is False and stored[1] == "null"
                await db.execute(
                    text(
                        "UPDATE public.project_templates SET label_config='{}'::jsonb "
                        "WHERE id=:id"
                    ),
                    {"id": created_template_id},
                )
            reset_response, reset_body = await request(
                "project_admin", "GET", f"{_ROUTE_PATH}/{created_template_id}"
            )
            assert reset_response.status_code == 200, reset_response.text
            assert reset_body["label_config"] == {}

            async with admin_maker.begin() as db:
                await db.execute(
                    text("UPDATE public.users SET is_active=false WHERE id=:id"),
                    {"id": actors["viewer"].id},
                )
            inactive_response, _ = await request(
                "viewer", "GET", _ROUTE_PATH, active=False
            )
            assert inactive_response.status_code == 401

        capture["direct_read_probes"] = [
            await _direct_read_probe(
                admin_maker=admin_maker,
                runtime_maker=runtime_maker,
                actor=actors["project_admin"],
                no_autoflush=no_autoflush,
            )
            for no_autoflush in (False, True)
        ]
        capture["timezone_read_probe"] = await _timezone_read_probe(
            runtime_maker,
            actors["project_admin"],
            template_by_alias["public_image"].id,
        )
        assert capture["timezone_read_probe"]["timezone_before_reads"] == (
            "Asia/Shanghai"
        )
        assert capture["timezone_read_probe"]["timezone_after_list"] == (
            "Asia/Shanghai"
        )
        assert capture["timezone_read_probe"]["timezone_after_detail"] == (
            "Asia/Shanghai"
        )
        expected_timestamp = (_BASE_TIME + timedelta(minutes=1)).isoformat()
        assert capture["timezone_read_probe"]["list_created_at_iso"] == (
            expected_timestamp
        )
        assert capture["timezone_read_probe"]["detail_created_at_iso"] == (
            expected_timestamp
        )
        assert [
            (
                row["included_in_route_result"],
                row["pending_in_session_new_after_read"],
                row["caller_transaction_open_after_read"],
                row["visible_from_owner_before_caller_rollback"],
            )
            for row in capture["direct_read_probes"]
        ] == [(True, False, True, False), (False, True, True, False)]

        async with admin_maker() as observer:
            persisted = await observer.get(ProjectTemplate, created_template_id)
            assert persisted is not None
            assert persisted.label_config == {}
            assert await observer.get(Project, project_ids[0]) is not None
            assert (
                await observer.get(
                    ProjectTemplate, template_by_alias["public_image"].id
                )
            ).usage_count == 6
    finally:
        if previous_db_override is _MISSING:
            app_module.dependency_overrides.pop(get_db, None)
        else:
            app_module.dependency_overrides[get_db] = previous_db_override
        await runtime_engine.dispose()
        if actor_ids:
            await _cleanup_owned(
                admin_maker,
                actor_ids=actor_ids,
                organization_ids=organization_ids,
                membership_ids=membership_ids,
                operation_ids=operation_ids,
            )

    _write_capture(capture)
