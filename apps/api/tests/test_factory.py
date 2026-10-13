"""Test fixture password reuse without changing production hashing."""

from app.core.security import hash_password, verify_password
from tests.conftest import _make_user
from tests.factory import make_user_dict


def test_fixture_password_reuse_keeps_real_password_verification():
    first = make_user_dict("employee", "first@e2e.test", "First")
    second = _make_user("viewer", "second@e2e.test", "Second")
    assert first["id"] != second["id"]
    assert second["role"] == "viewer"
    assert second["email"] == "second@e2e.test"
    assert first["password_hash"] == second["password_hash"]
    assert verify_password("Test1234", first["password_hash"])
    assert not verify_password("wrong-password", first["password_hash"])

    custom = make_user_dict("employee", "custom@e2e.test", "Custom", "Other1234")
    repeated = make_user_dict("employee", "again@e2e.test", "Again", "Other1234")
    assert verify_password("Other1234", custom["password_hash"])
    assert custom["password_hash"] != repeated["password_hash"]
    assert custom["password_hash"] != first["password_hash"]
    assert hash_password("Test1234") != first["password_hash"]
