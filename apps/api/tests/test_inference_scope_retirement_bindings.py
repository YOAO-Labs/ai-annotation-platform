"""Real SQLAlchemy, function-only DSN and remote API retirement assembly."""

import os

import pytest

from app.config import settings
from app.services.ml_routing.authority import RoutingAuthority
from tests.test_inference_scope_retirement_live import (
    test_retire_one_live_scope_preserves_another_and_fences_delayed_tokens as _exercise,
)

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1",
        reason="requires the verified disposable module-data environment",
    ),
]


@pytest.mark.parametrize("lost_ack", [False, True])
async def test_api_uses_remote_owner_for_live_scope_retirement(
    test_engine, tmp_path, monkeypatch, lost_ack
):
    def authority(owner_url, owner_token):
        for name, value in {
            "inference_routing_url": owner_url,
            "inference_routing_token": owner_token,
            "inference_routing_token_file": "",
        }.items():
            monkeypatch.setattr(settings, name, value)
        return RoutingAuthority()

    await _exercise(
        test_engine,
        tmp_path,
        "handler",
        lost_ack,
        retirement_authority_factory=authority,
    )
