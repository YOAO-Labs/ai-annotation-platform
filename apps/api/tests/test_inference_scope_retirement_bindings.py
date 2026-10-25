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


@pytest.mark.parametrize("binding", ("embedded", "dsn", "http"))
async def test_api_selected_retirement_source_uses_the_actual_backend(
    test_engine, tmp_path, monkeypatch, binding
):
    def authority(owner_url, owner_token):
        for name, value in {
            "inference_routing_url": owner_url if binding == "http" else "",
            "inference_routing_token": owner_token if binding == "http" else "",
            "inference_routing_token_file": "",
            "inference_routing_database_url": (
                os.environ["AAP_MODULE_DATA_INFERENCE_ROUTING_URL"]
                if binding == "dsn"
                else ""
            ),
            "inference_routing_database_url_file": "",
        }.items():
            monkeypatch.setattr(settings, name, value)
        return RoutingAuthority(test_engine)

    await _exercise(
        test_engine,
        tmp_path,
        "handler",
        True,
        retirement_authority_factory=authority,
    )
