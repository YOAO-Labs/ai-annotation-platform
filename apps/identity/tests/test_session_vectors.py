"""Known answers generated independently with Node's standard crypto primitives."""

from pathlib import Path
import json

from aap_identity.session_contract import SessionClaims
from aap_identity.session_manager import (
    login_request_fingerprint,
    session_derivation,
    sign_session_claims,
)


def test_session_issuance_matches_language_independent_known_answer():
    vector = json.loads(
        (Path(__file__).parent / "fixtures/session_vectors.json").read_text()
    )
    claims = SessionClaims.model_validate_json(json.dumps(vector["claims"]))
    assert session_derivation(vector["secret"])[1] == vector["derivation_id"]
    assert (
        login_request_fingerprint(vector["secret"], vector["email"], vector["password"])
        == vector["request_fingerprint"]
    )
    assert sign_session_claims(claims, vector["secret"]) == vector["access_token"]
