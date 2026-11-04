import numpy as np
import pytest

from yoao_protocol_v2 import encode_low_res_mask
from app.services.ai_mask_session import (
    AiMaskSessionError,
    MASK_SESSION_TTL_SECONDS,
    issue_ai_mask_session,
    verify_ai_mask_session,
)


def test_mask_session_roundtrip_and_expiry() -> None:
    raw = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    token = issue_ai_mask_session(
        raw,
        {"task_id": "task-1", "frame_index": 4, "model_id": "sam"},
        now=100,
    )
    claims = verify_ai_mask_session(token, now=100)
    assert claims["raw"] == raw
    assert claims["frame_index"] == 4
    assert raw not in token
    assert "task-1" not in token

    with pytest.raises(AiMaskSessionError) as caught:
        verify_ai_mask_session(token, now=100 + MASK_SESSION_TTL_SECONDS)
    assert caught.value.reason == "mask_session_expired"


def test_mask_session_rejects_tampering() -> None:
    raw = encode_low_res_mask(np.zeros((256, 256), dtype=np.float32))
    token = issue_ai_mask_session(raw, {"task_id": "task-1"}, now=100)
    replacement = "A" if token[-1] != "A" else "B"
    with pytest.raises(AiMaskSessionError) as caught:
        verify_ai_mask_session(f"{token[:-1]}{replacement}", now=100)
    assert caught.value.reason == "invalid_mask_session"
