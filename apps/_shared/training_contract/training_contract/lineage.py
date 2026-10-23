"""Validate frozen reference provenance without consulting current storage state."""

from copy import deepcopy
from datetime import datetime
import re
import uuid

from .dataset import DatasetContractError

SOURCE_LINEAGE_CONTRACT = {"schema": 1, "scope": "accepted-reference"}
_FIELDS = {
    "source_id",
    "location_id",
    "path_digest",
    "scan_run_id",
    "accepted_revision_id",
    "revision_number",
    "previous_revision_id",
    "accepted_at",
    "scan_completed_at",
    "content_verified_at",
    "source_config_revision",
    "location_config_revision",
}
_HASH = re.compile(r"^[a-f0-9]{64}$")


def _invalid(message):
    raise DatasetContractError("invalid_source_lineage", message)


def _declared(record):
    if not isinstance(record, dict):
        _invalid("冻结记录必须保留结构化契约")
    if "source_lineage_contract" not in record:
        return False
    contract = record["source_lineage_contract"]
    if (
        not isinstance(contract, dict)
        or type(contract.get("schema")) is not int
        or contract != SOURCE_LINEAGE_CONTRACT
    ):
        raise DatasetContractError(
            "unknown_source_lineage_contract", "未知的冻结来源证据契约"
        )
    return True


def _uuid(value, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str):
        _invalid("冻结来源标识必须是规范 UUID")
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        _invalid("冻结来源标识必须是规范 UUID")
    if parsed != value:
        _invalid("冻结来源标识必须是规范 UUID")


def _timestamp(value):
    if value is None:
        return
    if not isinstance(value, str) or len(value) > 64 or "T" not in value:
        _invalid("冻结来源时间必须是带时区的 ISO 时间或未知")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _invalid("冻结来源时间必须是带时区的 ISO 时间或未知")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _invalid("冻结来源时间必须是带时区的 ISO 时间或未知")


def _member(member, declared):
    if not isinstance(member, dict):
        _invalid("冻结成员必须保留结构化来源身份")
    if "source_lineage" not in member:
        if declared and member.get("asset_reference") is not None:
            _invalid("引用成员缺少声明的冻结来源证据")
        return None
    if not declared:
        _invalid("冻结来源证据缺少显式契约")
    if member.get("asset_reference") is None:
        _invalid("冻结来源证据必须属于引用成员")
    value = member["source_lineage"]
    if not isinstance(value, dict) or set(value) != _FIELDS:
        _invalid("冻结来源证据字段不完整或包含未授权的定位信息")
    for key in ("source_id", "location_id", "scan_run_id"):
        _uuid(value[key])
    for key in ("accepted_revision_id", "previous_revision_id"):
        _uuid(value[key], nullable=True)
    if not isinstance(value["path_digest"], str) or not _HASH.fullmatch(
        value["path_digest"]
    ):
        _invalid("冻结来源路径摘要必须是 SHA-256")
    for key in ("source_config_revision", "location_config_revision"):
        if type(value[key]) is not int or value[key] < 1:
            _invalid("冻结来源配置修订必须是正整数")
    revision = value["revision_number"]
    if revision is not None and (type(revision) is not int or revision < 1):
        _invalid("冻结内容修订必须是正整数或未知")
    for key in ("accepted_at", "scan_completed_at", "content_verified_at"):
        _timestamp(value[key])
    return value


def validate_source_lineage(record):
    """Reject unknown declarations and unsafe member fields before consumption."""
    declared = _declared(record)
    members = record.get("samples", [])
    if not isinstance(members, list):
        _invalid("冻结成员必须是列表")
    for member in members:
        _member(member, declared)


def source_lineage_evidence(record, member):
    """Interpret one frozen member in constant time, including historical gaps."""
    value = _member(member, _declared(record))
    if value is None:
        return {
            "status": "unrecorded",
            "reasons": [
                "historical_contract_missing"
                if member.get("asset_reference") is not None
                else "not_reference"
            ],
            "evidence": None,
        }
    reasons = []
    if any(
        value[key] is None
        for key in ("accepted_revision_id", "revision_number", "accepted_at")
    ):
        reasons.append("accepted_revision_unknown")
    if value["scan_completed_at"] is None:
        reasons.append("scan_time_unknown")
    if value["content_verified_at"] is None:
        reasons.append("verification_time_unknown")
    return {
        "status": "partial" if reasons else "recorded",
        "reasons": reasons,
        "evidence": deepcopy(value),
    }
