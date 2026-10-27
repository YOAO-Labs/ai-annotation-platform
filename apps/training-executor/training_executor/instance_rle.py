"""Qualified bitmap consumer with bounded instance and semantic targets."""

from __future__ import annotations

from copy import copy
import hashlib
import inspect
import json
from pathlib import Path
import random

import cv2
import numpy as np
from PIL import Image, ImageOps
import torch
from torch.utils.data import DataLoader, Dataset
from ultralytics.data.augment import RandomHSV
from ultralytics.data.dataset import YOLODataset
from ultralytics.models.yolo.segment import SegmentationTrainer, SegmentationValidator
from ultralytics.utils.loss import v8SegmentationLoss

from training_contract.dataset import (
    INSTANCE_RLE_LIMITS,
    instance_rle_budget,
)
from training_contract.observability import InputMismatchError
from .files import atomic_json

MAX_LABEL_BYTES = INSTANCE_RLE_LIMITS["max_sample_label_bytes"]
MAX_SEMANTIC_CLASS_PIXELS = 64_000_000


def semantic_target(masks, classes):
    """Match native Format: smallest covering instance, first input on area ties."""
    height, width = masks.shape[-2:]
    result = np.zeros((height, width), dtype=np.float32)
    smallest = np.full((height, width), height * width + 1, dtype=np.int64)
    for mask, native_class in zip(masks, classes, strict=True):
        area = int(mask.sum())
        covered = (mask != 0) & (area < smallest)
        result[covered] = native_class
        smallest[covered] = area
    return torch.from_numpy(result)


def decode_rle(rle):
    """One original-size uint8 buffer, retained only until nearest resize."""
    height, width = rle["size"]
    flat = np.empty(height * width, dtype=np.uint8)
    offset = 0
    for index, count in enumerate(rle["counts"]):
        flat[offset : offset + count] = index % 2
        offset += count
    return flat.reshape((height, width), order="F")


def transform_mask(rle, imgsz, *, flip=False):
    height, width = rle["size"]
    ratio = min(imgsz / height, imgsz / width)
    resized = (round(width * ratio), round(height * ratio))
    left, top = (imgsz - resized[0]) // 2, (imgsz - resized[1]) // 2
    original = decode_rle(rle)
    scaled = cv2.resize(original, resized, interpolation=cv2.INTER_NEAREST)
    del original
    target = np.zeros((imgsz, imgsz), dtype=np.uint8)
    target[top : top + resized[1], left : left + resized[0]] = scaled
    del scaled
    return np.ascontiguousarray(target[:, ::-1]) if flip else target


def mask_box(mask):
    ys, xs = np.nonzero(mask)
    if not len(xs):
        return None
    height, width = mask.shape
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    return (
        (x0 + x1) / (2 * width),
        (y0 + y1) / (2 * height),
        (x1 - x0) / width,
        (y1 - y0) / height,
    )


def read_payload(path):
    if path.stat().st_size > MAX_LABEL_BYTES:
        raise InputMismatchError("Frozen RLE sample exceeds encoded object budget")
    return json.loads(path.read_text())


class InstanceRLEDataset(Dataset):
    """No original-size mask cache, worker processes, prefetch or contour stage."""

    rect = False
    cache = False
    mosaic = False

    def __init__(self, root, args, split, batch_size, *, augment=None):
        self.root, self.args, self.split = Path(root), args, split
        self.augment = split == "train" if augment is None else augment
        self.imgsz = args.imgsz
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.semantic = getattr(args, "nms", None) is False
        if (
            self.semantic
            and batch_size * len(manifest["classes"]) * self.imgsz**2
            > MAX_SEMANTIC_CLASS_PIXELS
        ):
            raise InputMismatchError(
                "Semantic targets exceed 64M class pixels; reduce batch, classes or imgsz"
            )
        self.samples = [
            sample for sample in manifest["samples"] if sample["split"] == split
        ]
        self.im_files = [
            str(self.root / sample["image_path"]) for sample in self.samples
        ]
        self.labels = []
        for sample in self.samples:
            instance_rle_budget(
                sample["width"], sample["height"], len(sample["instances"])
            )
            self.labels.append(
                {
                    "cls": np.array(
                        [[item["class_index"]] for item in sample["instances"]],
                        dtype=np.float32,
                    ).reshape(-1, 1),
                    "bboxes": np.array(
                        [
                            [
                                (item["bbox_xyxy"][0] + item["bbox_xyxy"][2])
                                / (2 * sample["width"]),
                                (item["bbox_xyxy"][1] + item["bbox_xyxy"][3])
                                / (2 * sample["height"]),
                                (item["bbox_xyxy"][2] - item["bbox_xyxy"][0])
                                / sample["width"],
                                (item["bbox_xyxy"][3] - item["bbox_xyxy"][1])
                                / sample["height"],
                            ]
                            for item in sample["instances"]
                        ],
                        dtype=np.float32,
                    ).reshape(-1, 4),
                    "im_file": str(self.root / sample["image_path"]),
                    "shape": (sample["height"], sample["width"]),
                }
            )
        largest = sorted(
            (len(sample["instances"]) for sample in self.samples), reverse=True
        )[:batch_size]
        if (
            sum(largest) * self.imgsz**2
            > INSTANCE_RLE_LIMITS["max_batch_mask_elements"]
        ):
            raise InputMismatchError(
                "RLE batch exceeds 64M training mask elements; reduce batch or imgsz"
            )
        self.hsv = RandomHSV(args.hsv_h, args.hsv_s, args.hsv_v)

    def __len__(self):
        return len(self.samples)

    def close_mosaic(self, hyp=None):
        pass

    def __getitem__(self, index):
        return self.item(index, augment=self.augment)

    def item(self, index, *, augment=False):
        sample = self.samples[index]
        payload = read_payload(self.root / sample["label_path"])
        height, width = sample["height"], sample["width"]
        with Image.open(self.im_files[index]) as source:
            image = np.asarray(ImageOps.exif_transpose(source).convert("RGB"))[
                :, :, ::-1
            ].copy()
        if image.shape[:2] != (height, width):
            raise InputMismatchError(
                "Frozen raster image dimensions differ from mask coordinates"
            )
        ratio = min(self.imgsz / height, self.imgsz / width)
        resized = (round(width * ratio), round(height * ratio))
        left, top = (self.imgsz - resized[0]) // 2, (self.imgsz - resized[1]) // 2
        image = cv2.resize(image, resized, interpolation=cv2.INTER_LINEAR)
        image = cv2.copyMakeBorder(
            image,
            top,
            self.imgsz - resized[1] - top,
            left,
            self.imgsz - resized[0] - left,
            cv2.BORDER_CONSTANT,
            value=(114, 114, 114),
        )
        flip = augment and random.random() < self.args.fliplr
        if augment:
            image = self.hsv({"img": image})["img"]
        if flip:
            image = image[:, ::-1]
        if augment and random.random() < self.args.bgr:
            image = image[:, :, ::-1]
        masks = np.empty(
            (len(payload["instances"]), self.imgsz, self.imgsz), dtype=np.uint8
        )
        boxes, classes = [], []
        for position, instance in enumerate(payload["instances"]):
            mask = transform_mask(instance["rle"], self.imgsz, flip=flip)
            box = mask_box(mask)
            if box is None:
                raise InputMismatchError(
                    f"Instance {instance['annotation_id']} vanishes at training resolution"
                )
            masks[position] = mask
            del mask
            boxes.append(box)
            classes.append([instance["class_index"]])
        count = len(boxes)
        return {
            "img": torch.from_numpy(
                np.ascontiguousarray(image[:, :, ::-1].transpose(2, 0, 1))
            ),
            "cls": torch.tensor(classes, dtype=torch.float32).reshape(-1, 1),
            "bboxes": torch.tensor(boxes, dtype=torch.float32).reshape(-1, 4),
            "masks": torch.from_numpy(masks),
            "sem_masks": semantic_target(masks, [item[0] for item in classes]),
            "batch_idx": torch.zeros(count),
            "im_file": self.im_files[index],
            "ori_shape": (height, width),
            "resized_shape": (self.imgsz, self.imgsz),
            "ratio_pad": ((resized[1] / height, resized[0] / width), (left, top)),
        }

    @staticmethod
    def collate_fn(batch):
        elements = sum(item["masks"].numel() for item in batch)
        if elements > INSTANCE_RLE_LIMITS["max_batch_mask_elements"]:
            raise InputMismatchError("RLE batch exceeds 64M training mask elements")
        return YOLODataset.collate_fn(batch)

    def preflight(self):
        """Stream originals; detect loss of tiny instances before optimization."""
        vanished, validation_vanished = [], []
        vanished_count = validation_vanished_count = 0
        for sample in self.samples:
            payload = read_payload(self.root / sample["label_path"])
            for instance in payload["instances"]:
                mask = transform_mask(instance["rle"], self.imgsz)
                identity = {
                    "task_id": sample["task_id"],
                    "annotation_id": instance["annotation_id"],
                    "annotation_version": instance["annotation_version"],
                }
                if not mask.any():
                    vanished_count += 1
                    if len(vanished) < 1000:
                        vanished.append(identity)
                # This is the actual pinned SegmentationValidator target resize.
                target = torch.nn.functional.interpolate(
                    torch.from_numpy(mask)[None, None].float(),
                    (self.imgsz // 4, self.imgsz // 4),
                    mode="bilinear",
                    align_corners=False,
                ).gt_(0.5)
                if mask.any() and not target.any():
                    validation_vanished_count += 1
                    if len(validation_vanished) < 1000:
                        validation_vanished.append(identity)
        return {
            "samples": len(self.samples),
            "resize_vanished": vanished,
            "resize_vanished_count": vanished_count,
            "validator_vanished_count": validation_vanished_count,
            "detail_limit": 1000,
            "validator_vanished": validation_vanished,
            "mask_plane": [self.imgsz, self.imgsz],
            "validator_plane": [self.imgsz // 4, self.imgsz // 4],
        }


class InstanceRLEValidator(SegmentationValidator):
    def build_dataset(self, img_path, mode="val", batch=None):
        return InstanceRLEDataset(
            Path(img_path).parent.parent,
            self.args,
            self.args.split,
            batch or self.args.batch,
            augment=False,
        )


class InstanceRLETrainer(SegmentationTrainer):
    def build_dataset(self, img_path, mode="train", batch=None):
        return InstanceRLEDataset(
            Path(img_path).parent.parent, self.args, mode, batch or self.batch_size
        )

    def get_dataloader(self, dataset_path, batch_size=16, rank=0, mode="train"):
        if rank not in {-1, 0} or self.args.workers != 0:
            raise InputMismatchError(
                "RLE first revision requires single-device workers=0"
            )
        dataset = self.build_dataset(dataset_path, mode, batch_size)
        evidence = dataset.preflight()
        atomic_json(self.save_dir / f"rle-{mode}-preflight.json", evidence)
        atomic_json(self.save_dir.parent / f"rle-{mode}-preflight.json", evidence)
        if evidence["resize_vanished"]:
            raise InputMismatchError(
                "Instances disappear at selected input resolution; inspect RLE preflight"
            )
        return DataLoader(
            dataset,
            batch_size=min(batch_size, len(dataset)),
            shuffle=mode == "train",
            num_workers=0,
            pin_memory=False,
            collate_fn=dataset.collate_fn,
        )

    def get_validator(self):
        return InstanceRLEValidator(
            self.test_loader,
            save_dir=self.save_dir,
            args=copy(self.args),
            _callbacks=self.callbacks,
        )


def consumer_evidence():
    return {
        "bridge": "ultralytics-instance-rle-1",
        "workers": 0,
        "prefetch": False,
        "transform": "aap-letterbox-nearest-1",
        "rasterizer": "aap-pixel-center-even-odd-1",
        "loss_mask_processing": "truth-full-input-plane;prototype-bilinear-upsample-align-corners-false",
        "validator_mask_processing": "truth-bilinear-align-corners-false-threshold-0.5-at-input-quarter",
        "loss_source_sha256": hashlib.sha256(
            inspect.getsource(v8SegmentationLoss.loss).encode()
        ).hexdigest(),
        "validator_source_sha256": hashlib.sha256(
            inspect.getsource(SegmentationValidator._prepare_batch).encode()
        ).hexdigest(),
        "limits": dict(INSTANCE_RLE_LIMITS),
        "semantic_target": "smallest-covering-instance;first-input-on-equal-area;background-zero",
        "max_semantic_class_pixels": MAX_SEMANTIC_CLASS_PIXELS,
    }
