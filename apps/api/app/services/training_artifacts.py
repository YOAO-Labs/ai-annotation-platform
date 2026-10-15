"""Bounded verified JSON reads; cached parsing never replaces storage verification."""

import copy
import hashlib
import json
import math
import threading
from collections import OrderedDict

from app.services.storage import storage_service as storage

MAX_REPORT_BYTES = 8 * 1024 * 1024
PARSER_REVISION = 1
_cache = OrderedDict()
_cache_bytes = 0
_lock = threading.Lock()


def _bounded(value, depth=0, budget=None):
    budget = budget if budget is not None else [100000]
    budget[0] -= 1
    if budget[0] < 0 or depth > 16:
        raise ValueError("Evaluation structure exceeds limits")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Evaluation contains non-finite numbers")
    if isinstance(value, dict):
        for key, item in value.items():
            if len(key) > 200:
                raise ValueError("Evaluation key exceeds limits")
            _bounded(item, depth + 1, budget)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, budget)
    elif isinstance(value, str) and len(value) > 8192:
        raise ValueError("Evaluation string exceeds limits")


def read_json_artifact(spec, attempt_id):
    global _cache_bytes
    if not spec or not 0 < spec["size_bytes"] <= MAX_REPORT_BYTES:
        raise ValueError("Evaluation artifact is missing or too large")
    response = storage.client.get_object(
        Bucket=storage.training_bucket, Key=spec["key"]
    )
    try:
        raw = response["Body"].read(spec["size_bytes"] + 1)
        if (
            len(raw) != spec["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != spec["sha256"]
            or response.get("Metadata", {}).get("attempt-id") != str(attempt_id)
        ):
            raise ValueError("Evaluation artifact identity mismatch")
    finally:
        response["Body"].close()
    key = (spec["sha256"], PARSER_REVISION)
    with _lock:
        if key in _cache:
            result, _ = _cache[key]
            _cache.move_to_end(key)
            return copy.deepcopy(result)
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Evaluation must be an object")
    _bounded(result)
    with _lock:
        if key not in _cache:
            while _cache and (
                _cache_bytes + len(raw) > 32 * 1024 * 1024 or len(_cache) >= 64
            ):
                _, (_, size) = _cache.popitem(last=False)
                _cache_bytes -= size
            _cache[key] = (copy.deepcopy(result), len(raw))
            _cache_bytes += len(raw)
    return result
