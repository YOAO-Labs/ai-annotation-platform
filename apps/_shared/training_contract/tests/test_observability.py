import math

import pytest

from training_contract.observability import (
    LINE_BYTES,
    METRICS,
    RedactedLines,
    eta_basis,
    failure_receipt,
    measurement,
    redact_text,
    utf8_chunks,
    validate_sample,
)


def test_zero_is_observed_and_invalid_numeric_values_are_unavailable():
    assert measurement(0) == {"value": 0, "reason": None}
    for value in (True, False, -1, math.nan, math.inf, "0"):
        assert measurement(value)["value"] is None
    sample = {
        "phase": "training",
        "phase_elapsed_seconds": 0,
        "metrics": {key: measurement(0) for key in METRICS},
    }
    validate_sample(sample)
    sample["metrics"]["gpu_utilization"] = measurement(101)
    with pytest.raises(ValueError, match="Invalid observed"):
        validate_sample(sample)


@pytest.mark.parametrize(
    "source",
    [
        '{"password": "unknown-secret"}',
        "{'lease_token': 'unknown-secret'}",
        "authorization=Bearer unknown-secret",
        "Bearer unknown-secret",
        "X-Amz-Signature=unknown-secret",
        "api_token=unknown-secret",
    ],
)
def test_quoted_and_unquoted_credentials_are_removed(source):
    assert "unknown-secret" not in redact_text(source)


def test_redacts_before_chunking_and_normalizes_split_utf8_controls_and_crlf():
    raw = (
        "\x1b[31m中文\x1b[0m https://storage.test/file?X-Amz-Signature=signed "
        "token=other /state/private/train.py C:\\private\\file.py credential-value\r\nfinished\rnext"
    ).encode()
    redactor = RedactedLines(("credential-value",))
    output = []
    for byte in raw:
        output.extend(redactor.feed(bytes([byte])))
    output.extend(redactor.feed(b"", final=True))
    text = "".join(output)
    assert text.startswith("中文 ") and text.endswith("finished\nnext\n")
    for secret in (
        "signed",
        "storage.test",
        "other",
        "/state",
        "private",
        "credential-value",
        "\x1b",
        "\r",
    ):
        assert secret not in text
    chunks = list(utf8_chunks(text * 500))
    assert "".join(chunks) == text * 500
    assert all(len(chunk.encode()) <= 2048 for chunk in chunks)


def test_oversized_line_discards_whole_secret_prefix_and_suffix_until_delimiter():
    redactor = RedactedLines()
    assert redactor.feed(b"password=prefix" + b"a" * LINE_BYTES) == []
    lines = redactor.feed(b"suffix-secret\nretained\n")
    assert lines == ["[oversized native log line omitted]\n", "retained\n"]
    assert redactor.dropped_lines == 1
    assert redactor.feed(b"token=partial", final=True) == [
        "token=[credential redacted]\n"
    ]


def test_eta_excludes_warmup_requires_comparable_cycles_and_suppresses_regime_crossing():
    cycles = [{"seconds": 100, "comparable": False, "closing": False}]
    cycles.extend(
        {"seconds": value, "comparable": True, "closing": False}
        for value in (10, 12, 14)
    )
    basis = eta_basis(cycles, completed_epoch=4, total_epochs=6)
    assert basis["available"] and basis["sample_count"] == 3
    assert (basis["minimum_seconds"], basis["maximum_seconds"]) == (20, 28)
    assert not eta_basis(cycles[:3], completed_epoch=3, total_epochs=6)["available"]
    assert (
        eta_basis(cycles, completed_epoch=4, total_epochs=6, close_mosaic=2)["reason"]
        == "unobserved_augmentation_regime"
    )
    assert (
        eta_basis(cycles, completed_epoch=6, total_epochs=6)["reason"]
        == "training_complete"
    )


def test_failure_source_is_explicit_and_diagnostic_is_redacted():
    receipt = failure_receipt(
        "preparation_failed",
        "preparing",
        "a" * 64,
        '{"password": "private"} /state/input',
        exception_type="OSError",
    )
    assert receipt["source"] == "supervisor"
    assert (
        "private" not in receipt["diagnostic"] and "/state" not in receipt["diagnostic"]
    )
