"""Bounded artifact verification and cached JSON parsing."""

from app.services import storage_access
import copy
import hashlib
import json
import math
import threading
from collections import OrderedDict

from training_contract.observability import SUMMARY_BYTES

from app.services.storage import storage_service as storage

MAX_REPORT_BYTES = 8 * 1024 * 1024
PARSER_REVISION = 1
_cache = OrderedDict()
_cache_bytes = 0
_lock = threading.Lock()


def _bounded(value, depth=0, budget=None, string_budget=8192):
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
            _bounded(item, depth + 1, budget, string_budget)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, depth + 1, budget, string_budget)
    elif isinstance(value, str) and len(value) > string_budget:
        raise ValueError("Evaluation string exceeds limits")


def read_json_artifact(
    spec,
    attempt_id,
    *,
    bucket=None,
    transport=None,
    metadata_key="attempt-id",
    required_metadata=None,
    node_budget=100000,
    string_budget=8192,
    observation=None,
):
    global _cache_bytes
    transport = storage if transport is None else transport
    if not spec or not 0 < spec["size_bytes"] <= MAX_REPORT_BYTES:
        raise ValueError("Evaluation artifact is missing or too large")
    response = transport.client.get_object(
        Bucket=transport.training_bucket if bucket is None else bucket, Key=spec["key"]
    )
    try:
        raw = response["Body"].read(spec["size_bytes"] + 1)
        if (
            len(raw) != spec["size_bytes"]
            or hashlib.sha256(raw).hexdigest() != spec["sha256"]
            or response.get("Metadata", {}).get(metadata_key) != str(attempt_id)
            or any(
                response.get("Metadata", {}).get(key) != value
                for key, value in (required_metadata or {}).items()
            )
        ):
            raise ValueError("Evaluation artifact identity mismatch")
    finally:
        storage_access.close_registered(response["Body"])
    if observation is not None:
        observation.readback(response)
    key = (spec["sha256"], PARSER_REVISION, node_budget, string_budget)
    with _lock:
        if key in _cache:
            result, _ = _cache[key]
            _cache.move_to_end(key)
            return copy.deepcopy(result)
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Evaluation must be an object")
    _bounded(result, budget=[node_budget], string_budget=string_budget)
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


def verify_artifact(key, spec, attempt_id):
    response = storage.client.get_object(Bucket=storage.training_bucket, Key=key)
    try:
        if response["ContentLength"] != spec["size_bytes"] or response.get(
            "Metadata", {}
        ).get("attempt-id") != str(attempt_id):
            raise ValueError("Artifact size or identity mismatch")
        digest = hashlib.sha256()
        total = 0
        for chunk in iter(lambda: response["Body"].read(1024 * 1024), b""):
            digest.update(chunk)
            total += len(chunk)
            if total > spec["size_bytes"]:
                raise ValueError("Artifact size exceeded")
        if total != spec["size_bytes"] or digest.hexdigest() != spec["sha256"]:
            raise ValueError("Artifact digest mismatch")
    finally:
        response["Body"].close()


def verify_summary(run, attempt, artifact):
    if artifact["size_bytes"] > SUMMARY_BYTES:
        raise ValueError("Observation artifact exceeds its bound")
    response = storage.client.get_object(
        Bucket=storage.training_bucket, Key=artifact["key"]
    )
    try:
        body = json_load_bounded(response["Body"].read(SUMMARY_BYTES + 1))
    finally:
        response["Body"].close()
    if (
        body.get("protocol") != 1
        or body.get("run_id") != str(run.id)
        or body.get("attempt_id") != str(attempt.id)
        or body.get("config_digest") != run.config_digest
    ):
        raise ValueError("Observation artifact identity differs")


def json_load_bounded(raw):
    if len(raw) > SUMMARY_BYTES:
        raise ValueError("Observation artifact exceeds its bound")
    return json.loads(raw)
