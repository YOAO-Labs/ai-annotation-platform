"""Frozen members and observations of native loader admission, not epoch draws."""

from collections import Counter
from pathlib import PurePosixPath
import re
import uuid

from .catalog import digest

MAX_MEMBERS = 20000
SPLITS = ("train", "val")
MEMBER_FIELDS = (
    "task_id",
    "split",
    "image_path",
    "label_path",
    "sha256",
    "label_sha256",
    "width",
    "height",
    "size_bytes",
)
POLICY_DIGEST = digest(
    {
        "schema": 1,
        "scope": "native-loader-membership",
        "member_fields": MEMBER_FIELDS,
        "splits": SPLITS,
        "max_members": MAX_MEMBERS,
    }
)
_HASH = re.compile(r"^[a-f0-9]{64}$")


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def _path(value, kind, split):
    if (
        not isinstance(value, str)
        or len(value) > 1000
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError("Invalid frozen input member path")
    path = PurePosixPath(value)
    if (
        path.as_posix() != value
        or len(path.parts) != 3
        or path.parts[:2] != (kind, split)
        or ".." in path.parts
    ):
        raise ValueError("Invalid frozen input member path")
    return path


def member_records(manifest):
    """Index distinct logical members, retaining identical content in other tasks."""
    if not isinstance(manifest, dict):
        raise ValueError("Invalid frozen input manifest")
    samples = manifest.get("samples")
    if not isinstance(samples, list) or not 1 <= len(samples) <= MAX_MEMBERS:
        raise ValueError("Frozen input members are missing or exceed the version limit")
    records, labels = {}, set()
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("split") not in SPLITS:
            raise ValueError("Invalid frozen input member split")
        split = sample["split"]
        image = _path(sample.get("image_path"), "images", split)
        label = _path(sample.get("label_path"), "labels", split)
        if (
            not _uuid(sample.get("task_id"))
            or not _hash(sample.get("sha256"))
            or not _hash(sample.get("label_sha256"))
            or image.stem != label.stem
            or str(image) in records
            or str(label) in labels
        ):
            raise ValueError("Invalid or duplicate frozen input member")
        for field in ("width", "height", "size_bytes"):
            value = sample.get(field)
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError("Invalid frozen input member dimensions or size")
        member = {field: sample.get(field) for field in MEMBER_FIELDS}
        records[str(image)] = {
            "task_id": sample["task_id"],
            "member_digest": digest(member),
            "split": split,
        }
        labels.add(str(label))
    return records


def compact_members(records):
    return {
        split: sorted(
            (
                {key: row[key] for key in ("task_id", "member_digest")}
                for row in records.values()
                if row["split"] == split
            ),
            key=lambda row: (row["member_digest"], row["task_id"]),
        )
        for split in SPLITS
    }


def contract_for(manifest):
    """Historical manifests without a member list retain their original contract."""
    if not isinstance(manifest, dict):
        raise ValueError("Invalid frozen input manifest")
    if "samples" not in manifest:
        return None
    members = compact_members(member_records(manifest))
    if not all(members.values()):
        raise ValueError("Frozen training and validation members are required")
    return {
        "schema": 1,
        "policy_digest": POLICY_DIGEST,
        "members_digest": digest(members),
        "counts": {split: len(rows) for split, rows in members.items()},
    }


def declared_contract(config):
    if not isinstance(config, dict):
        raise ValueError("Invalid frozen training configuration")
    if "input_contract" not in config:
        return None
    value = config["input_contract"]
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "policy_digest", "members_digest", "counts"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["policy_digest"] != POLICY_DIGEST
        or not _hash(value["members_digest"])
        or not isinstance(value["counts"], dict)
        or set(value["counts"]) != set(SPLITS)
        or any(type(n) is not int or n < 1 for n in value["counts"].values())
        or sum(value["counts"].values()) > MAX_MEMBERS
    ):
        raise ValueError("Unknown or invalid frozen input contract")
    return value


def observed_members(records, split, paths):
    """Compare normalized native paths as a multiset, permitting native reordering."""
    if (
        split not in SPLITS
        or not isinstance(paths, list)
        or len(paths) > MAX_MEMBERS
        or any(not isinstance(path, str) for path in paths)
    ):
        raise ValueError("Invalid native input members")
    expected = [path for path, row in records.items() if row["split"] == split]
    if Counter(paths) != Counter(expected):
        raise ValueError("Native loader members differ from the frozen manifest")
    return compact_members({path: records[path] for path in paths})[split]


def verify_receipt(receipt, config, run_id, attempt_id):
    contract = declared_contract(config)
    fields = {
        "schema",
        "policy_digest",
        "run_id",
        "attempt_id",
        "config_digest",
        "dataset_id",
        "dataset_sha256",
        "members_digest",
        "members",
        "counts",
    }
    if contract is None or not isinstance(receipt, dict) or set(receipt) != fields:
        raise ValueError("Training input receipt is missing or invalid")
    if (
        not _uuid(str(run_id))
        or not _uuid(str(attempt_id))
        or not _uuid(config.get("dataset_id"))
        or not _hash(config.get("dataset_sha256"))
        or not isinstance(receipt["counts"], dict)
        or any(type(n) is not int for n in receipt["counts"].values())
    ):
        raise ValueError("Training input receipt identity is invalid")
    expected = {
        "schema": 1,
        "policy_digest": POLICY_DIGEST,
        "run_id": str(run_id),
        "attempt_id": str(attempt_id),
        "config_digest": digest(config),
        "dataset_id": config["dataset_id"],
        "dataset_sha256": config["dataset_sha256"],
        "members_digest": contract["members_digest"],
        "counts": contract["counts"],
    }
    if type(receipt["schema"]) is not int or any(
        receipt[key] != value for key, value in expected.items()
    ):
        raise ValueError("Training input receipt identity differs from its attempt")
    members = receipt["members"]
    if not isinstance(members, dict) or set(members) != set(SPLITS):
        raise ValueError("Training input receipt partitions are invalid")
    seen = set()
    normalized = {}
    for split in SPLITS:
        rows = members[split]
        if not isinstance(rows, list) or len(rows) != contract["counts"][split]:
            raise ValueError("Training input receipt member count differs")
        for row in rows:
            if (
                not isinstance(row, dict)
                or set(row) != {"task_id", "member_digest"}
                or not _uuid(row["task_id"])
                or not _hash(row["member_digest"])
                or row["member_digest"] in seen
            ):
                raise ValueError(
                    "Training input receipt has invalid or duplicate members"
                )
            seen.add(row["member_digest"])
        normalized[split] = sorted(
            rows, key=lambda row: (row["member_digest"], row["task_id"])
        )
    if digest(normalized) != contract["members_digest"]:
        raise ValueError("Training accepted members differ from the frozen manifest")
    return {
        "counts": dict(contract["counts"]),
        "members_digest": contract["members_digest"],
    }


def receipt_for(config, run_id, attempt_id, members):
    contract = declared_contract(config)
    if contract is None:
        raise ValueError("Training input receipt requires a frozen contract")
    result = {
        "schema": 1,
        "policy_digest": POLICY_DIGEST,
        "run_id": str(run_id),
        "attempt_id": str(attempt_id),
        "config_digest": digest(config),
        "dataset_id": config["dataset_id"],
        "dataset_sha256": config["dataset_sha256"],
        "members_digest": digest(members),
        "members": members,
        "counts": {split: len(members[split]) for split in SPLITS},
    }
    verify_receipt(result, config, run_id, attempt_id)
    return result
