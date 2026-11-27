"""Validate the embedded metadata of an auxiliary original."""

import json

from app.services.content_snapshot import ContentError

MAX_CONTRACT_BYTES = 65536


RAW_FIELDS = ("sample", "sample_data", "calibrated_sensor", "sensor", "ego_pose")


def reject(code="invalid_receipt", status=409):
    raise ContentError("auxiliary_original_" + code, status)


def contract(kind, value):
    """Validate the bounded original table contract, never a storage locator."""
    if kind not in {"raw_lidar", "map"} or not isinstance(value, dict):
        reject("contract_invalid")
    try:
        encoded = json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        reject("contract_invalid")
    if len(encoded.encode()) > MAX_CONTRACT_BYTES:
        reject("source_budget_exceeded", 422)
    if kind == "raw_lidar":
        if set(value) != set(RAW_FIELDS) or any(
            not isinstance(value[key], dict) for key in RAW_FIELDS
        ):
            reject("contract_invalid")
        sample, data, calibrated, sensor, pose = (value[key] for key in RAW_FIELDS)
        if (
            not all(
                row.get("token") for row in (sample, data, calibrated, sensor, pose)
            )
            or data.get("sample_token") != sample["token"]
            or data.get("calibrated_sensor_token") != calibrated["token"]
            or calibrated.get("sensor_token") != sensor["token"]
            or data.get("ego_pose_token") != pose["token"]
            or data.get("timestamp") != pose.get("timestamp")
            or data.get("is_key_frame") is not True
            or sensor.get("modality") != "lidar"
            or not isinstance(data.get("filename"), str)
            or not data["filename"].endswith(".pcd.bin")
        ):
            reject("contract_invalid")
    elif (
        not value.get("token")
        or not value.get("filename")
        or not isinstance(value.get("log_tokens"), list)
        or not value["log_tokens"]
        or not all(isinstance(key, str) and key for key in value["log_tokens"])
    ):
        reject("contract_invalid")
    return json.loads(encoded)
