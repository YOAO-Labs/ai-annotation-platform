"""Verify trainer-emitted input membership receipts before publication."""

import json
import uuid

import pytest
from training_contract.input_membership import contract_for, member_records, receipt_for
from training_contract.trainer_plugin import canonical_json_digest as digest
from training_executor.input_membership import verified_local_receipt
from training_executor.worker import Supervisor


def _frozen_fixture(tmp_path):
    samples = []
    for index, split in enumerate(("train", "train", "val")):
        image = f"images/{split}/{index}.png"
        label = f"labels/{split}/{index}.txt"
        samples.append(
            {
                "task_id": str(uuid.UUID(int=index + 1)),
                "split": split,
                "image_path": image,
                "label_path": label,
                "sha256": f"{index + 1:064x}",
                "label_sha256": f"{index + 10:064x}",
                "width": 64,
                "height": 32,
                "size_bytes": 128,
            }
        )
    manifest = {"samples": samples, "classes": ["shape"]}
    config = {
        "adapter_revision": 3,
        "dataset_id": str(uuid.uuid4()),
        "dataset_sha256": "d" * 64,
        "input_contract": contract_for(manifest),
    }
    records = member_records(manifest)
    members = {
        split: sorted(
            (
                {"task_id": row["task_id"], "member_digest": row["member_digest"]}
                for row in records.values()
                if row["split"] == split
            ),
            key=lambda row: (row["member_digest"], row["task_id"]),
        )
        for split in ("train", "val")
    }
    run_id = str(uuid.uuid4())
    attempt_id = str(uuid.uuid4())
    proof = receipt_for(config, run_id, attempt_id, members)
    (tmp_path / "input-membership.json").write_text(json.dumps(proof))
    return config, run_id, attempt_id, proof


def test_verified_local_receipt_rejects_missing_or_tampered_file(tmp_path):
    config, run_id, attempt_id, proof = _frozen_fixture(tmp_path)
    receipt_path = tmp_path / "input-membership.json"
    assert verified_local_receipt(config, run_id, attempt_id, receipt_path) == proof

    tampered = {**proof, "counts": {"train": 99, "val": 1}}
    receipt_path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="identity differs"):
        verified_local_receipt(config, run_id, attempt_id, receipt_path)

    receipt_path.unlink()
    with pytest.raises(ValueError, match="missing"):
        verified_local_receipt(config, run_id, attempt_id, receipt_path)


def test_success_inventory_embeds_receipt_and_failure_can_settle_without_one(tmp_path):
    config, run_id, attempt_id, proof = _frozen_fixture(tmp_path)
    supervisor = object.__new__(Supervisor)
    supervisor.runtime = {}
    journal = {
        "outcome": "succeeded",
        "claim": {"runtime": {}},
        "assignment": {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "config": config,
            "config_digest": digest(config),
        },
        "plugin_artifacts": [],
    }
    inventory = supervisor.inventory(journal, tmp_path)
    assert (
        json.loads((tmp_path / "manifest.json").read_text())["input_membership"]
        == proof
    )
    assert [item["name"] for item in inventory] == ["manifest.json"]
    (tmp_path / "input-membership.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        supervisor.inventory(journal, tmp_path)
    journal["outcome"] = "cancelled"
    supervisor.inventory(journal, tmp_path)
    assert "input_membership" not in json.loads(
        (tmp_path / "manifest.json").read_text()
    )
