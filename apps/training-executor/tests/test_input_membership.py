"""Compare actual native datasets and keep the receipt inside the execution manifest."""

import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest
from training_contract.catalog import digest
from training_contract.input_membership import contract_for
from training_contract.observability import InputMismatchError
from training_executor.input_membership import InputMembership, membership_for
from training_executor.worker import Supervisor


@pytest.fixture
def frozen(tmp_path):
    from PIL import Image

    samples = []
    for index, split in enumerate(("train", "train", "val")):
        image, label = f"images/{split}/{index}.png", f"labels/{split}/{index}.txt"
        (tmp_path / image).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / label).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (48 + index * 16, 32), "white").save(tmp_path / image)
        (tmp_path / label).write_text("0 0.5 0.5 0.5 0.5\n")
        samples.append(
            {
                "task_id": str(uuid.UUID(int=index + 1)),
                "split": split,
                "image_path": image,
                "label_path": label,
                "sha256": hashlib.sha256((tmp_path / image).read_bytes()).hexdigest(),
                "label_sha256": hashlib.sha256(
                    (tmp_path / label).read_bytes()
                ).hexdigest(),
                "width": 48 + index * 16,
                "height": 32,
                "size_bytes": (tmp_path / image).stat().st_size,
                "instances": [],
            }
        )
    manifest = {"samples": samples, "classes": ["shape"]}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    (tmp_path / "data.yaml").write_text(
        "train: images/train\nval: images/val\nnames: ['shape']\n"
    )
    config = {
        "dataset_id": str(uuid.uuid4()),
        "dataset_sha256": "d" * 64,
        "input_contract": contract_for(manifest),
    }
    spec = {
        "data_yaml": str(tmp_path / "data.yaml"),
        "frozen_config": config,
        "run_id": str(uuid.uuid4()),
        "attempt_id": str(uuid.uuid4()),
    }
    return spec, manifest, tmp_path


def test_legacy_contract_is_not_backfilled_and_explicit_bad_contract_fails(tmp_path):
    assert membership_for({"frozen_config": {}}, tmp_path) is None
    with pytest.raises(InputMismatchError):
        membership_for({"frozen_config": {"input_contract": None}}, tmp_path)


@pytest.mark.parametrize(
    "change", ["missing", "duplicate", "extra", "outside", "split"]
)
def test_bad_actual_paths_cannot_produce_a_receipt(frozen, change):
    spec, manifest, root = frozen
    observer = InputMembership(spec, root, manifest)
    paths = [root / sample["image_path"] for sample in manifest["samples"][:2]]
    if change == "missing":
        paths.pop()
    elif change == "duplicate":
        paths[1] = paths[0]
    elif change == "extra":
        paths.append(root / "images/train/extra.png")
    elif change == "outside":
        paths[0] = root.parent / "elsewhere.png"
    else:
        paths[1] = root / manifest["samples"][-1]["image_path"]
    with pytest.raises(InputMismatchError):
        observer.observe("train", paths)
    with pytest.raises(InputMismatchError):
        observer.complete()
    assert not (root / "input-membership.json").exists()


@pytest.mark.parametrize("task", ["detect", "segment", "instance_rle", "libreyolo"])
def test_real_native_datasets_are_observed_without_replacing_sampling(frozen, task):
    from torch.utils.data import DataLoader

    spec, manifest, root = frozen
    if task in {"segment", "instance_rle"}:
        for sample in manifest["samples"]:
            if task == "segment":
                text = "0 0.25 0.25 0.75 0.25 0.75 0.75 0.25 0.75\n"
            else:
                sample["label_path"] = sample["label_path"].replace(".txt", ".json")
                text = json.dumps(
                    {
                        "schema": 1,
                        "size": [sample["height"], sample["width"]],
                        "instances": [],
                    }
                )
            (root / sample["label_path"]).write_text(text)
            sample["label_sha256"] = hashlib.sha256(text.encode()).hexdigest()
        (root / "manifest.json").write_text(json.dumps(manifest))
        spec["frozen_config"]["input_contract"] = contract_for(manifest)
    observer = InputMembership(spec, root, manifest)
    for split in ("train", "val"):
        if task == "libreyolo":
            from libreyolo.data.dataset import YOLODataset
            from libreyolo.data.augment.yolo9 import YOLO9MosaicMixupDataset

            dataset = YOLODataset(
                data_dir=str(root), split=split, img_size=128, num_classes=1
            )
            if split == "train":
                dataset = YOLO9MosaicMixupDataset(dataset, 128)
                loader = DataLoader(dataset, batch_size=1)
                paths = loader.dataset.dataset.img_files
            else:
                loader = DataLoader(dataset, batch_size=1)
                paths = loader.dataset.img_files
        elif task == "instance_rle":
            from ultralytics.cfg import get_cfg
            from training_executor.instance_rle import InstanceRLEDataset

            dataset = InstanceRLEDataset(
                root, get_cfg(overrides={"imgsz": 128}), split, 1
            )
            loader = DataLoader(dataset, batch_size=1)
            paths = loader.dataset.im_files
        else:
            from ultralytics.data.dataset import YOLODataset

            dataset = YOLODataset(
                img_path=str(root / "images" / split),
                data={"names": {0: "shape"}},
                task=task,
                imgsz=128,
                augment=False,
                rect=True,
                batch_size=1,
            )
            loader = DataLoader(dataset, batch_size=1)
            paths = loader.dataset.im_files
        observer.observe(split, paths)
        observer.observe(split, list(reversed(paths)))
    proof = observer.complete()
    assert proof["counts"] == {"train": 2, "val": 1}
    assert (
        proof["members_digest"]
        == spec["frozen_config"]["input_contract"]["members_digest"]
    )


def test_ultralytics_silent_corrupt_image_filter_is_a_membership_failure(frozen):
    from ultralytics.data.dataset import YOLODataset

    spec, manifest, root = frozen
    # A frozen byte hash alone does not prove the native loader admitted the file.
    path = root / manifest["samples"][0]["image_path"]
    path.write_bytes(b"not an image")
    manifest["samples"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    spec["frozen_config"]["input_contract"] = contract_for(manifest)
    observer = InputMembership(spec, root, manifest)
    dataset = YOLODataset(
        img_path=str(root / "images/train"),
        data={"names": {0: "shape"}},
        task="detect",
        imgsz=128,
        augment=False,
    )
    assert len(dataset.im_files) == 1
    with pytest.raises(InputMismatchError, match="differ"):
        observer.observe("train", dataset.im_files)


def test_success_inventory_embeds_receipt_and_failure_can_settle_without_one(frozen):
    spec, manifest, root = frozen
    observer = InputMembership(spec, root, manifest)
    for split in ("train", "val"):
        observer.observe(
            split,
            [
                root / row["image_path"]
                for row in manifest["samples"]
                if row["split"] == split
            ],
        )
    proof = observer.complete()
    supervisor = object.__new__(Supervisor)
    supervisor.runtime = {}
    journal = {
        "outcome": "succeeded",
        "claim": {"runtime": {}},
        "assignment": {
            "run_id": spec["run_id"],
            "attempt_id": spec["attempt_id"],
            "config": spec["frozen_config"],
            "config_digest": digest(spec["frozen_config"]),
        },
    }
    inventory = supervisor.inventory(journal, root)
    assert json.loads((root / "manifest.json").read_text())["input_membership"] == proof
    assert [item["name"] for item in inventory] == ["manifest.json"]
    (root / "input-membership.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        supervisor.inventory(journal, root)
    journal["outcome"] = "cancelled"
    supervisor.inventory(journal, root)
    assert "input_membership" not in json.loads((root / "manifest.json").read_text())


def test_libreyolo_callback_checks_actual_wrapped_train_and_lazy_val(frozen):
    from training_executor.libreyolo_adapter import CheckpointCallbacks

    spec, manifest, root = frozen
    observer = InputMembership(spec, root, manifest)
    callbacks = CheckpointCallbacks(spec, root, membership=observer)
    paths = [root / row["image_path"] for row in manifest["samples"][:2]]
    callbacks.trainer = SimpleNamespace(
        train_loader=SimpleNamespace(
            dataset=SimpleNamespace(dataset=SimpleNamespace(img_files=paths[:-1]))
        )
    )
    with pytest.raises(InputMismatchError, match="differ"):
        callbacks.on_train_start(SimpleNamespace())
    callbacks.trainer._epoch_validator = SimpleNamespace(
        dataloader=SimpleNamespace(dataset=SimpleNamespace(img_files=paths))
    )
    with pytest.raises(InputMismatchError, match="differ"):
        callbacks.on_train_epoch_end(SimpleNamespace())


@pytest.mark.parametrize("stage", ["train", "val", "final_val"])
def test_ultralytics_adapter_hooks_reject_changed_loader_before_success(
    frozen, monkeypatch, stage
):
    import ultralytics
    from training_executor import train as adapter

    spec, manifest, root = frozen
    spec.update(
        framework="ultralytics", parameters={"epochs": 3}, base_path="/unused/base.pt"
    )
    spec["frozen_config"].update(
        framework="ultralytics",
        framework_version=ultralytics.__version__,
        adapter_revision=1,
        base_model={"id": "yolo11n"},
    )
    observer = InputMembership(spec, root, manifest)
    paths = {
        split: [
            str(root / row["image_path"])
            for row in manifest["samples"]
            if row["split"] == split
        ]
        for split in ("train", "val")
    }

    def loader(split, missing=False):
        return SimpleNamespace(
            dataset=SimpleNamespace(
                im_files=paths[split][:-1] if missing else paths[split]
            )
        )

    class Model:
        task = "detect"
        model = None

        def __init__(self, *args, **kwargs):
            self.callbacks = {}

        def add_callback(self, name, callback):
            self.callbacks[name] = callback

        def train(self, **kwargs):
            trainer = SimpleNamespace(
                train_loader=loader("train", stage == "train"),
                test_loader=loader("val", stage == "val"),
                args=SimpleNamespace(close_mosaic=0),
                optimizer=SimpleNamespace(param_groups=[]),
                model=SimpleNamespace(named_parameters=lambda: []),
            )
            self.callbacks["on_train_start"](trainer)
            self.callbacks["on_val_start"](
                SimpleNamespace(dataloader=loader("val", stage == "final_val"))
            )
            raise AssertionError("Membership mismatch must interrupt native completion")

    monkeypatch.setattr(ultralytics, "YOLO", Model)
    monkeypatch.setattr(adapter, "validate_architecture", lambda *args, **kwargs: None)
    with pytest.raises(InputMismatchError, match="differ"):
        adapter.ultralytics_train(spec, root, membership=observer)
    assert not (root / "result.json").exists()


def test_independent_train_evaluator_observes_its_own_loader(frozen):
    from training_executor.train import UltralyticsTrainingEvaluation

    spec, manifest, root = frozen
    observer = InputMembership(spec, root, manifest)

    class Validator:
        args = SimpleNamespace()
        dataloader = SimpleNamespace(dataset=SimpleNamespace(rect=False))

        def __init__(self, **kwargs):
            pass

        def get_dataloader(self, *args):
            return SimpleNamespace(dataset=SimpleNamespace(im_files=[]))

    trainer = SimpleNamespace(
        validator=Validator(),
        data={"train": "unused"},
        device="cpu",
        stride=32,
        model=SimpleNamespace(names=["shape"]),
        batch_size=1,
    )
    with pytest.raises(InputMismatchError, match="differ"):
        UltralyticsTrainingEvaluation(root, observer).evaluate(trainer)
