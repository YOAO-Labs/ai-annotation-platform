"""Bounded observations and redaction, independent of training execution authority."""

from __future__ import annotations

import codecs
import json
import math
import re
from datetime import datetime, timezone

PROTOCOL = 1
REDACTION_REVISION = 1
SAMPLE_LIMIT = 720
LOG_LIMIT = 512
CHUNK_BYTES = 2048
LINE_BYTES = 8192
REQUEST_BYTES = 128 * 1024
STATE_BYTES = 16 * 1024
LOG_BYTES = 8 * 1024 * 1024
SUMMARY_BYTES = 256 * 1024
PHASES = {"preparing", "training", "evaluating", "uploading"}
METRICS = {
    "cpu_cores": {"unit": "cores", "scope": "process_group", "source": "psutil"},
    "rss_bytes": {"unit": "bytes", "scope": "process_group_sum", "source": "psutil"},
    "gpu_utilization": {
        "unit": "percent",
        "scope": "physical_device",
        "source": "nvidia-smi",
    },
    "gpu_used_mib": {"unit": "MiB", "scope": "physical_device", "source": "nvidia-smi"},
    "gpu_total_mib": {
        "unit": "MiB",
        "scope": "physical_device",
        "source": "nvidia-smi",
    },
    "process_gpu_utilization": {
        "unit": "percent",
        "scope": "native_process",
        "source": "unavailable",
    },
    "cuda_allocated_bytes": {
        "unit": "bytes",
        "scope": "child_allocator",
        "source": "torch",
    },
    "cuda_reserved_bytes": {
        "unit": "bytes",
        "scope": "child_allocator",
        "source": "torch",
    },
    "images_per_second": {
        "unit": "images/s",
        "scope": "native_batches",
        "source": "native_callback",
    },
}
REASONS = {
    "baseline_pending",
    "process_unavailable",
    "permission_denied",
    "pid_reused",
    "device_unavailable",
    "invalid_value",
    "pid_namespace_unresolved",
    "cuda_uninitialized",
    "callback_unavailable",
    "callback_stale",
    "recovery_no_process",
}
FAILURE_CODES = {
    "input_mismatch",
    "config_mismatch",
    "runtime_mismatch",
    "cuda_oom",
    "native_exception",
    "native_signal",
    "preparation_failed",
    "upload_failed",
    "execution_lost",
    "authority_lost",
    "unknown",
    "budget_exhausted",
}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def measurement(value=None, reason=None):
    if type(value) in {int, float} and math.isfinite(value) and value >= 0:
        return {"value": value, "reason": None}
    return {"value": None, "reason": reason if reason in REASONS else "invalid_value"}


def validate_sample(sample):
    """Strict numeric/source vocabulary; transport validates identities and cursors."""
    if sample.get("phase") not in PHASES:
        raise ValueError("Invalid observation phase")
    elapsed = sample.get("phase_elapsed_seconds")
    if type(elapsed) not in {int, float} or not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Invalid phase duration")
    values = sample.get("metrics", {})
    if set(values) != set(METRICS):
        raise ValueError("Unexpected observation metric vocabulary")
    for key, item in values.items():
        if set(item) != {"value", "reason"}:
            raise ValueError("Invalid metric receipt")
        value, reason = item["value"], item["reason"]
        if value is None:
            if reason not in REASONS:
                raise ValueError("Unavailable metric needs an explicit reason")
        elif (
            type(value) not in {int, float}
            or not math.isfinite(value)
            or value < 0
            or reason is not None
            or (key.endswith("utilization") and value > 100)
        ):
            raise ValueError("Invalid observed metric")
    if len(encoded(sample)) > STATE_BYTES:
        raise ValueError("Observation exceeds its bound")
    return sample


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL = re.compile(r"(?i)\b(?:https?|s3|gs)://[^\s<>\"']+")
_CREDENTIAL = re.compile(
    r"(?i)(\bbearer\s+|\b(?:[\w-]*(?:token|secret|password|credential|signature)|"
    r"x-amz-[\w-]+|authorization)[\"']?\s*[=:]\s*)"
    r"(?:bearer\s+)?(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\"']+)"
)
_PATH = re.compile(r"(?<![\w/])(?:[A-Za-z]:[\\/]|/)[^\s<>\"'\]\[),;]+")


def redact_text(text, secrets=()):
    text = _ANSI.sub("", text)
    text = _CONTROL.sub("", text)
    for secret in sorted(
        (str(value) for value in secrets if value), key=len, reverse=True
    ):
        text = text.replace(secret, "[credential redacted]")
    text = _URL.sub("[URL redacted]", text)
    text = _CREDENTIAL.sub(lambda match: match[1] + "[credential redacted]", text)
    return _PATH.sub("[path redacted]", text)


class RedactedLines:
    """Never emit a prefix/suffix of an oversized raw line or incomplete UTF-8."""

    def __init__(self, secrets=()):
        self.secrets = tuple(secrets)
        self.pending = bytearray()
        self.dropping = False
        self.dropped_lines = 0
        self._after_cr = False

    def feed(self, chunk: bytes, *, final=False):
        lines = []
        for byte in chunk:
            if byte in {10, 13}:
                if byte == 10 and self._after_cr:
                    self._after_cr = False
                    continue
                self._after_cr = byte == 13
                lines.append(self._line())
            else:
                self._after_cr = False
                if not self.dropping:
                    self.pending.append(byte)
                    if len(self.pending) > LINE_BYTES:
                        self.pending.clear()
                        self.dropping = True
        if final and (self.pending or self.dropping):
            lines.append(self._line())
        return lines

    def _line(self):
        if self.dropping:
            self.dropped_lines += 1
            result = "[oversized native log line omitted]"
        else:
            result = redact_text(
                bytes(self.pending).decode("utf-8", errors="replace"), self.secrets
            )
        self.pending.clear()
        self.dropping = False
        return result + "\n"


def utf8_chunks(text, maximum=CHUNK_BYTES):
    remaining = text.encode("utf-8")
    while remaining:
        decoder = codecs.getincrementaldecoder("utf-8")()
        piece = decoder.decode(remaining[:maximum], final=False)
        consumed = len(piece.encode("utf-8"))
        yield piece
        remaining = remaining[consumed:]


class InputMismatchError(ValueError):
    pass


class NativeConfigurationMismatch(ValueError):
    pass


class NativeRuntimeMismatch(NativeConfigurationMismatch):
    pass


def failure_receipt(
    code,
    stage,
    config_digest,
    diagnostic,
    *,
    exception_type=None,
    secrets=(),
    source="supervisor",
):
    if code not in FAILURE_CODES or stage not in PHASES | {"execution"}:
        raise ValueError("Unknown failure classification")
    return {
        "code": code,
        "stage": stage,
        "config_digest": config_digest,
        "diagnostic": redact_text(str(diagnostic), secrets)[:1000],
        "exception_type": str(exception_type)[:120] if exception_type else None,
        "source": source,
    }


def eta_basis(cycles, *, completed_epoch, total_epochs, close_mosaic=0):
    """Comparable complete cycles only; never promise evaluation/upload duration."""
    unavailable = {
        "available": False,
        "reason": "insufficient_comparable_cycles",
        "sample_count": 0,
    }
    if (
        type(completed_epoch) is not int
        or type(total_epochs) is not int
        or completed_epoch >= total_epochs
    ):
        return {**unavailable, "reason": "training_complete"}
    closing_start = (
        total_epochs - close_mosaic + 1 if close_mosaic else total_epochs + 1
    )
    closing = completed_epoch >= closing_start
    if close_mosaic and not closing:
        return {**unavailable, "reason": "unobserved_augmentation_regime"}
    comparable = [
        item["seconds"]
        for item in cycles[-20:]
        if item.get("comparable") is True
        and item.get("closing") is closing
        and type(item.get("seconds")) in {int, float}
        and math.isfinite(item["seconds"])
        and item["seconds"] > 0
    ]
    if len(comparable) < 3:
        return {**unavailable, "sample_count": len(comparable)}
    remaining = total_epochs - completed_epoch
    return {
        "available": True,
        "sample_count": len(comparable),
        "remaining_epochs": remaining,
        "minimum_seconds": min(comparable) * remaining,
        "maximum_seconds": max(comparable) * remaining,
        "scope": "training_only",
        "closing": closing,
    }
