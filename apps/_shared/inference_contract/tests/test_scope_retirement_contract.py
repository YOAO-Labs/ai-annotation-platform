from __future__ import annotations

import pytest
import uuid

from aap_inference_contract.scope_retirement import (
    SCOPE_RETIREMENT_PROTOCOL,
    SCOPE_RETIREMENT_RESULT_PROTOCOL,
    ScopeRetirementResult,
    ScopeRetirementUnavailable,
    ScopeRetirementView,
    decode_scope_retirement_request,
    decode_scope_retirement_result,
    make_scope_retirement_request,
    scope_retirement_request_bytes,
    scope_retirement_result_bytes,
)

_RETIREMENT_ID = uuid.UUID("e469fa57-ff59-42ae-9fe7-366f895931f7")
_SCOPE_ID = uuid.UUID("c864b2a8-83f3-4f6a-bb40-f4ef8f0512bf")
_REGISTRY_ID = uuid.UUID("d4e6676d-9fe4-4c33-9ca5-fc1c31230052")
_EPOCH_ID = uuid.UUID("6a7a2139-2428-4c28-b4c8-6b0c40034289")
_INCARNATION_ID = uuid.UUID("be189eb2-2e7d-4c36-94b0-20ad78d68147")


def _request():
    return make_scope_retirement_request(
        _RETIREMENT_ID,
        _SCOPE_ID,
        "private-retirement-capability",
    )


def _view():
    return ScopeRetirementView(
        retirement_id=_RETIREMENT_ID,
        scope_id=_SCOPE_ID,
        registry_id=_REGISTRY_ID,
        authority_epoch=_EPOCH_ID,
        backend_incarnation=_INCARNATION_ID,
        phase="draining",
        closed=True,
    )


def test_request_is_exact_canonical_and_hides_capability_from_repr():
    request = _request()
    encoded = scope_retirement_request_bytes(request)

    assert decode_scope_retirement_request(encoded) == request
    assert request.model_dump(mode="json", by_alias=True) == {
        "schema": SCOPE_RETIREMENT_PROTOCOL,
        "retirement_id": str(_RETIREMENT_ID),
        "scope_id": str(_SCOPE_ID),
        "capability": "private-retirement-capability",
    }
    assert "private-retirement-capability" not in repr(request)


def test_request_rejects_extra_duplicate_and_noncanonical_fields():
    encoded = scope_retirement_request_bytes(_request()).decode("utf-8")
    with pytest.raises(ScopeRetirementUnavailable):
        decode_scope_retirement_request(encoded[:-1] + ',"extra":true}')
    with pytest.raises(ScopeRetirementUnavailable):
        decode_scope_retirement_request(
            encoded.replace(
                '"scope_id":',
                '"retirement_id":"' + str(_RETIREMENT_ID) + '","scope_id":',
                1,
            )
        )
    with pytest.raises(ScopeRetirementUnavailable):
        decode_scope_retirement_request(
            {
                "schema": SCOPE_RETIREMENT_PROTOCOL,
                "retirement_id": str(_RETIREMENT_ID).upper(),
                "scope_id": str(_SCOPE_ID),
                "capability": "token",
            }
        )


def test_result_binds_immutable_identity_and_unknown_is_null():
    request = _request()
    result = ScopeRetirementResult(
        schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
        retirement_id=_RETIREMENT_ID,
        scope_id=_SCOPE_ID,
        state="committed",
        result=_view(),
    )
    encoded = scope_retirement_result_bytes(result)

    decoded = decode_scope_retirement_result(request, encoded)
    assert decoded.result == _view()
    assert decoded.state == "committed"

    unknown = ScopeRetirementResult(
        schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
        retirement_id=_RETIREMENT_ID,
        scope_id=_SCOPE_ID,
        state="unknown",
        result=None,
    )
    assert (
        decode_scope_retirement_result(
            request, scope_retirement_result_bytes(unknown)
        ).result
        is None
    )

    mismatched = ScopeRetirementResult(
        schema=SCOPE_RETIREMENT_RESULT_PROTOCOL,
        retirement_id=_RETIREMENT_ID,
        scope_id=_SCOPE_ID,
        state="committed",
        result=_view(),
    ).model_dump(mode="json", by_alias=True)
    mismatched["scope_id"] = str(_REGISTRY_ID)
    with pytest.raises(ScopeRetirementUnavailable):
        decode_scope_retirement_result(request, mismatched)


@pytest.mark.parametrize("closed", (False, 0, 1, "true", None))
def test_result_requires_a_literal_boolean_closure(closed):
    request = _request()
    result = {
        "schema": SCOPE_RETIREMENT_RESULT_PROTOCOL,
        "retirement_id": str(_RETIREMENT_ID),
        "scope_id": str(_SCOPE_ID),
        "state": "committed",
        "result": {**_view().model_dump(mode="json"), "closed": closed},
    }
    with pytest.raises(ScopeRetirementUnavailable):
        decode_scope_retirement_result(request, result)
