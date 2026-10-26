from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import json

import pytest
from aap_protocol_v2.lifecycle import (
    AdmissionScope,
    AdmissionTokenClaims,
    verify_admission_token,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from aap_inference.gpu.settings import GPUArbiterMode, GPUAuthoritySettings
from aap_inference.gpu.signing import (
    GPUAdmissionSignerConfigError,
    GPUAdmissionTokenSigner,
)


def _encode_private_key(private_key: Ed25519PrivateKey) -> str:
    raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _claims() -> AdmissionTokenClaims:
    return AdmissionTokenClaims(
        backend_registry_id="backend-1",
        gpu_resource_id="node-a/GPU-1",
        boot_id="boot-1",
        generation="7",
        control_epoch="3",
        scope=AdmissionScope.PREDICT,
        jti="lease-1",
        exp=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp()),
    )


def test_signer_selects_active_key_and_emits_verifiable_token(tmp_path) -> None:
    old_key = Ed25519PrivateKey.generate()
    current_key = Ed25519PrivateKey.generate()
    key_file = tmp_path / "gpu-signing-keys.json"
    key_file.write_text(
        json.dumps(
            {
                "old": _encode_private_key(old_key),
                "current": _encode_private_key(current_key),
            }
        ),
        encoding="utf-8",
    )

    signer = GPUAdmissionTokenSigner.from_file(str(key_file), "current")
    token = signer.sign(_claims())

    assert signer.active_kid == "current"
    assert (
        verify_admission_token(
            token,
            keyring={"current": current_key.public_key()},
        )
        == _claims()
    )


def test_settings_keep_signer_file_lazy(monkeypatch) -> None:
    config = GPUAuthoritySettings(
        gpu_arbiter_mode=GPUArbiterMode.OFF,
        gpu_lifecycle_signing_keys_file="/must-not-be-read",
        gpu_lifecycle_active_signing_kid="current",
    )

    def fail_open(*_args, **_kwargs):
        raise AssertionError("off/observe Settings construction must not read secrets")

    monkeypatch.setattr("pathlib.Path.open", fail_open)

    assert config.gpu_lifecycle_signing_keys_file == "/must-not-be-read"


@pytest.mark.parametrize(
    ("body", "active_kid", "message"),
    [
        ("", "current", "empty"),
        ("[]", "current", "non-empty JSON object"),
        ("{}", "current", "non-empty JSON object"),
        ('{"current":"bad"}', "current", "private seed"),
        ('{"bad kid":"' + "A" * 43 + '"}', "current", "key ids"),
        ('{"current":"' + "A" * 42 + '="}', "current", "private seed"),
        ('{"current":"' + "_" * 43 + '"}', "current", "invalid"),
        ('{"old":"' + "A" * 43 + '"}', "current", "not present"),
    ],
)
def test_signer_rejects_malformed_key_files(
    tmp_path,
    body: str,
    active_kid: str,
    message: str,
) -> None:
    key_file = tmp_path / "invalid.json"
    key_file.write_text(body, encoding="utf-8")

    with pytest.raises(GPUAdmissionSignerConfigError, match=message):
        GPUAdmissionTokenSigner.from_file(str(key_file), active_kid)


def test_signer_rejects_duplicate_kids(tmp_path) -> None:
    private_key = _encode_private_key(Ed25519PrivateKey.generate())
    key_file = tmp_path / "duplicate.json"
    key_file.write_text(
        '{"current":"' + private_key + '","current":"' + private_key + '"}',
        encoding="utf-8",
    )

    with pytest.raises(GPUAdmissionSignerConfigError, match="duplicate"):
        GPUAdmissionTokenSigner.from_file(str(key_file), "current")


def test_signer_rejects_non_utf8_oversized_and_excessive_key_files(tmp_path) -> None:
    non_utf8 = tmp_path / "non-utf8.json"
    non_utf8.write_bytes(b"\xff")
    with pytest.raises(GPUAdmissionSignerConfigError, match="UTF-8") as caught:
        GPUAdmissionTokenSigner.from_file(str(non_utf8), "current")
    assert caught.value.__cause__ is None

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b" " * (64 * 1024 + 1))
    with pytest.raises(GPUAdmissionSignerConfigError, match="too large"):
        GPUAdmissionTokenSigner.from_file(str(oversized), "current")

    private_key = _encode_private_key(Ed25519PrivateKey.generate())
    excessive = tmp_path / "excessive.json"
    excessive.write_text(
        json.dumps({f"key-{index}": private_key for index in range(65)}),
        encoding="utf-8",
    )
    with pytest.raises(GPUAdmissionSignerConfigError, match="too many"):
        GPUAdmissionTokenSigner.from_file(str(excessive), "key-0")


def test_signer_rejects_missing_or_unavailable_configuration(tmp_path) -> None:
    with pytest.raises(GPUAdmissionSignerConfigError, match="not configured"):
        GPUAdmissionTokenSigner.from_file("", "current")
    with pytest.raises(GPUAdmissionSignerConfigError, match="missing or invalid"):
        GPUAdmissionTokenSigner.from_file(str(tmp_path / "keys.json"), "")
    with pytest.raises(GPUAdmissionSignerConfigError, match="unavailable"):
        GPUAdmissionTokenSigner.from_file(str(tmp_path / "missing.json"), "current")


def test_signer_errors_do_not_expose_key_material(tmp_path) -> None:
    secret = "super-secret-private-key-material"
    key_file = tmp_path / "invalid.json"
    key_file.write_text('{"current":"' + secret, encoding="utf-8")

    with pytest.raises(GPUAdmissionSignerConfigError) as caught:
        GPUAdmissionTokenSigner.from_file(str(key_file), "current")

    assert secret not in str(caught.value)
    assert caught.value.__cause__ is None
