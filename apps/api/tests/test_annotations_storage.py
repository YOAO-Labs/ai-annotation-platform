"""Annotation-file addresses are closed facts, independent of current binding."""

from dataclasses import replace
from types import SimpleNamespace
import uuid

import pytest

from app.services import annotations_storage as storage
from app.services.dataset_assets import LEGACY_LOCATION_ID
from app.services.managed_s3_target import Target


def logical_keys():
    first, second = uuid.uuid4(), uuid.uuid4()
    return (
        f"__aap_task_originals__/{first}/{second}/payload",
        f"raster-masks/objects/{first}.json",
        f"raster-masks/objects/{first}.json.gz",
        f"comment-attachments/{first}/{second}",
        f"projects/{first}/guide/{second}",
    )


def reference(logical=None, *, legacy=False, generation=2, root="公司/ 空间"):
    logical = logical or logical_keys()[0]
    target = Target(
        LEGACY_LOCATION_ID if legacy else uuid.uuid4(),
        1 if legacy else 3,
        None if legacy else 5,
        storage._runtime_endpoint() if legacy else "https://objects.example.test",
        "saved-annotations",
        "" if legacy else storage.managed_prefix(root),
        purpose="annotations",
    )
    return storage.make_ref(target, generation, target.key_prefix + logical), logical


@pytest.mark.parametrize("logical", logical_keys())
@pytest.mark.parametrize("legacy,generation", [(True, 1), (True, 3), (False, 2)])
def test_all_four_families_preserve_their_logical_keys(logical, legacy, generation):
    ref, logical = reference(logical, legacy=legacy, generation=generation)
    saved = storage.resolve_ref(ref, logical)
    assert saved.logical_key == logical
    assert saved.object_key == saved.key_prefix + logical
    assert len(saved.ref) == 10 and saved.ref["schema"] == 2
    ref["bucket"] = "changed"
    assert saved.bucket == "saved-annotations"


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema", True),
        ("schema", 1),
        ("purpose", "datasets"),
        ("binding_generation", 0),
        ("config_revision", True),
        ("key_prefix", "elsewhere/"),
        ("object_key", "elsewhere/"),
        ("secret", "forbidden"),
        ("storage_identity", "f" * 64),
    ],
)
def test_invalid_saved_ref_cannot_redirect(field, value):
    ref, logical = reference()
    ref[field] = value
    with pytest.raises(storage.AnnotationsStorageError):
        storage.resolve_ref(ref, logical)


def test_root_budgets_reserve_the_longest_task_suffix():
    namespace = storage.MANAGED_NAMESPACE
    logical = logical_keys()[0]
    assert len(logical) == 104
    ref, _ = reference(logical, root="a" * (896 - len(namespace) - 1))
    assert len(storage.resolve_ref(ref).object_key) == 1000
    with pytest.raises(storage.AnnotationsStorageError):
        storage.managed_prefix("a" * (897 - len(namespace) - 1))
    unicode_base = "界" * 12 + "a" * (896 - len(namespace) - 1 - 12)
    assert len(storage.managed_prefix(unicode_base).encode()) == 920
    with pytest.raises(storage.AnnotationsStorageError):
        storage.managed_prefix("界" + unicode_base)
    with pytest.raises(storage.AnnotationsStorageError):
        storage.resolve_ref(ref, logical_keys()[0])


async def test_current_credentials_preserve_saved_address(monkeypatch):
    ref, _ = reference()
    saved = storage.resolve_ref(ref)
    current = Target(
        saved.location_id,
        8,
        9,
        saved.endpoint_url,
        saved.bucket,
        saved.key_prefix,
        purpose="annotations",
    )

    class DB:
        async def get(self, *args, **kwargs):
            return SimpleNamespace(id=saved.location_id)

    async def target(*args, **kwargs):
        return current

    monkeypatch.setattr(storage, "target_for", target)
    assert (await storage.target_for_ref(DB(), ref)).connection_revision == 9
    current = replace(current, bucket="redirected")
    with pytest.raises(storage.AnnotationsStorageError):
        await storage.target_for_ref(DB(), ref)


async def test_allocation_stays_in_callers_transaction(db_session):
    logical = logical_keys()[0]
    ref = await storage.allocate(db_session, logical)
    saved = storage.resolve_ref(ref, logical)
    assert saved.location_id == LEGACY_LOCATION_ID
    assert saved.bucket == storage.storage_service.bucket
    assert db_session.in_transaction()
