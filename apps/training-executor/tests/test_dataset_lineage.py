"""The native input gate validates provenance without changing media checks."""

from copy import deepcopy
import hashlib
import json
import uuid

import pytest

from training_contract.dataset import DatasetContractError
from training_contract.lineage import SOURCE_LINEAGE_CONTRACT
from training_executor.adapters import validate_dataset


def dataset(tmp_path):
    (tmp_path / "data.yaml").write_text(
        'train: images/train\nval: images/val\nnames: {"0": "car"}\n'
    )
    (tmp_path / "classes.json").write_text('["car"]')
    members = []
    for index, split in enumerate(("train", "val"), 1):
        image = tmp_path / f"images/{split}/{index}.png"
        label = tmp_path / f"labels/{split}/{index}.txt"
        image.parent.mkdir(parents=True)
        label.parent.mkdir(parents=True)
        image.write_bytes(split.encode())
        label.write_text("0 0.5 0.5 0.2 0.2\n")
        members.append(
            {
                "split": split,
                "image_path": str(image.relative_to(tmp_path)),
                "label_path": str(label.relative_to(tmp_path)),
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "label_sha256": hashlib.sha256(label.read_bytes()).hexdigest(),
            }
        )
    manifest = {"schema": 1, "classes": ["car"], "samples": members}
    spec = {
        "data_yaml": str(tmp_path / "data.yaml"),
        "frozen_config": {"classes": ["car"]},
    }
    return spec, manifest


def evidence():
    return {
        "source_id": str(uuid.UUID(int=1)),
        "location_id": str(uuid.UUID(int=2)),
        "path_digest": "a" * 64,
        "scan_run_id": str(uuid.UUID(int=3)),
        "accepted_revision_id": None,
        "revision_number": None,
        "previous_revision_id": None,
        "accepted_at": None,
        "scan_completed_at": None,
        "content_verified_at": "2026-10-24T00:00:00+00:00",
        "source_config_revision": 1,
        "location_config_revision": 2,
    }


def test_native_input_accepts_legacy_and_partial_reference_evidence(tmp_path):
    spec, manifest = dataset(tmp_path)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    validate_dataset(spec)
    manifest["source_lineage_contract"] = dict(SOURCE_LINEAGE_CONTRACT)
    manifest["samples"][0].update(
        asset_reference={"schema": 1}, source_lineage=evidence()
    )
    path.write_text(json.dumps(manifest))
    validate_dataset(spec)
    (tmp_path / manifest["samples"][0]["image_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="missing or changed"):
        validate_dataset(spec)


@pytest.mark.parametrize("failure", ["contract", "private", "missing", "orphan"])
def test_native_input_refuses_unknown_or_downgraded_lineage(tmp_path, failure):
    spec, manifest = dataset(tmp_path)
    manifest["source_lineage_contract"] = deepcopy(SOURCE_LINEAGE_CONTRACT)
    member = manifest["samples"][0]
    member.update(asset_reference={"schema": 1}, source_lineage=evidence())
    if failure == "contract":
        manifest["source_lineage_contract"]["schema"] = 2
    elif failure == "private":
        member["source_lineage"]["source_url"] = "https://private.invalid/secret"
    elif failure == "missing":
        del member["source_lineage"]
    else:
        del manifest["source_lineage_contract"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(DatasetContractError):
        validate_dataset(spec)
