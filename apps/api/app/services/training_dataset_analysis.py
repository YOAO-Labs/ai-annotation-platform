"""Analyze only retained, frozen package members; never consult live annotations."""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
import uuid
from collections import Counter

from botocore.exceptions import BotoCoreError, ClientError
from PIL import Image, ImageOps
from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import defer

from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.training_dataset_analysis import (
    TrainingDatasetAnalysis,
    TrainingDatasetAnalyzedSample,
)
from app.services.storage import storage_service as storage
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    contract_of,
    native_labels,
    semantic_partitions,
)
from app.services.training_datasets import MAX_IMAGE_BYTES, MAX_SAMPLES
from training_contract.dataset import DETECTION, SEGMENTATION, polygon_area

ANALYSIS_SCHEMA = 1
HASH = re.compile(r"^[a-f0-9]{64}$")
CHANGES = {
    "added",
    "removed",
    "labels_changed",
    "split_changed",
    "multiplicity_changed",
    "task_ids_changed",
    "reintroduced",
    "unchanged",
    "unknown",
}


def valid_hash(value):
    return value if isinstance(value, str) and HASH.fullmatch(value) else None


def positive_int(value):
    return value if type(value) is int and value > 0 else None


def empty_sample(task_id, *, included):
    return {
        "task_id": uuid.UUID(str(task_id)),
        "included": included,
        "exclusion_reason": None,
        "issue": None,
        "sha256": None,
        "label_sha256": None,
        "semantic_label_digest": None,
        "split": None,
        "width": None,
        "height": None,
        "size_bytes": None,
        "instances": None,
        "is_negative": None,
        "explicit_negative": None,
        "class_ids": [],
        "labels": [],
        "media_state": "unchecked",
        "media_checked_at": None,
    }


def derive(
    snapshot: dict,
    manifest: dict,
    preparation_report: dict | None,
    snapshot_digest: str,
) -> dict:
    members = manifest.get("samples", [])
    if len(members) > MAX_SAMPLES or len(snapshot.get("samples", [])) > MAX_SAMPLES:
        raise TrainingDatasetError(
            "analysis_limit", "冻结样本数量超过分析范围", status_code=409
        )
    classes = manifest.get("classes", [])
    if (
        not isinstance(classes, list)
        or any(not isinstance(c, str) for c in classes)
        or len(set(classes)) != len(classes)
    ):
        raise TrainingDatasetError(
            "analysis_classes", "冻结类别表不可用", status_code=409
        )
    frozen = {str(s["task_id"]): s for s in snapshot.get("samples", [])}
    try:
        contract = contract_of(manifest)
    except TrainingDatasetError:
        contract = None
    segmentation = bool(contract and contract["task"] == SEGMENTATION)
    class_rows = [
        {
            "index": i,
            "name": name,
            "train_images": 0,
            "val_images": 0,
            "train_instances": 0,
            "val_instances": 0,
        }
        for i, name in enumerate(classes)
    ]
    splits = {
        key: {
            "images": 0,
            "instances": 0,
            "negatives": 0,
            "explicit_negatives": 0,
            "unknown_labels": 0,
        }
        for key in ("train", "val")
    }
    area_bins = [
        {"key": key, "label": label, "train": 0, "val": 0}
        for key, label in (
            ("small", "< 1% 图像面积"),
            ("medium", "1%–10% 图像面积"),
            ("large", "≥ 10% 图像面积"),
        )
    ]
    dimension_bins = [
        {"key": key, "label": label, "train": 0, "val": 0}
        for key, label in (
            ("small", "最长边 < 640 px"),
            ("medium", "最长边 640–1279 px"),
            ("large", "最长边 ≥ 1280 px"),
        )
    ]
    samples, contents, widths, heights = [], {}, [], []
    unknown_labels = unknown_dimensions = unknown_contents = 0
    observations = Counter()
    seen = set()
    for member in members:
        task_id = str(member["task_id"])
        if task_id in seen:
            raise TrainingDatasetError(
                "analysis_members", "冻结成员身份重复，无法安全分析", status_code=409
            )
        seen.add(task_id)
        row = empty_sample(task_id, included=True)
        row.update(
            sha256=valid_hash(member.get("sha256")),
            label_sha256=valid_hash(member.get("label_sha256")),
            split=member.get("split") if member.get("split") in splits else None,
            width=positive_int(member.get("width")),
            height=positive_int(member.get("height")),
            size_bytes=positive_int(member.get("size_bytes")),
        )
        split = row["split"]
        if split:
            splits[split]["images"] += 1
        else:
            observations["unknown_split"] += 1
        if row["width"] and row["height"]:
            widths.append(row["width"])
            heights.append(row["height"])
            longest = max(row["width"], row["height"])
            if split:
                dimension_bins[0 if longest < 640 else 1 if longest < 1280 else 2][
                    split
                ] += 1
        else:
            unknown_dimensions += 1
        source = frozen.get(task_id)
        try:
            if (
                not source
                or "annotations" not in source
                or snapshot.get("classes") != classes
                or contract is None
                or contract_of(snapshot) != contract
            ):
                raise TrainingDatasetError(
                    "frozen_labels_unavailable", "冻结标注或类别映射未保留"
                )
            text, instances = native_labels(source["annotations"], classes, contract)
            if segmentation and member.get("instances") != instances:
                raise TrainingDatasetError(
                    "frozen_instances_mismatch", "冻结实例来源与训练包不一致"
                )
            if (
                not row["label_sha256"]
                or hashlib.sha256(text.encode()).hexdigest() != row["label_sha256"]
            ):
                raise TrainingDatasetError(
                    "frozen_labels_mismatch", "冻结标签与训练包摘要不一致"
                )
            labels = []
            for line_number, line in enumerate(text.splitlines()):
                if segmentation:
                    values = line.split()
                    index = int(values[0])
                    coords = list(map(float, values[1:]))
                    points = [coords[i : i + 2] for i in range(0, len(coords), 2)]
                    labels.append(
                        {
                            "type": "polygon",
                            "class_index": index,
                            "class_name": classes[index],
                            "points": points,
                            "area": polygon_area(points),
                            "annotation_id": instances[line_number]["annotation_id"],
                            "annotation_version": instances[line_number][
                                "annotation_version"
                            ],
                        }
                    )
                    continue
                index, x, y, w, h = line.split()
                index = int(index)
                labels.append(
                    {
                        "class_index": index,
                        "class_name": classes[index],
                        "x_center": float(x),
                        "y_center": float(y),
                        "width": float(w),
                        "height": float(h),
                    }
                )
            row.update(
                labels=labels,
                instances=len(labels),
                is_negative=not labels,
                explicit_negative=(
                    True
                    if source.get("eligibility", {}).get("negative") is True
                    else None
                )
                if not labels
                else False,
                class_ids=sorted({label["class_index"] for label in labels}),
                semantic_label_digest=content_digest(
                    {
                        "data_contract": contract,
                        "labels": sorted(
                            (label["class_name"], label["points"]) for label in labels
                        ),
                    }
                    if segmentation
                    else sorted(
                        (
                            label["class_name"],
                            label["x_center"],
                            label["y_center"],
                            label["width"],
                            label["height"],
                        )
                        for label in labels
                    )
                ),
            )
            if split:
                splits[split]["instances"] += len(labels)
                splits[split]["negatives"] += int(not labels)
                splits[split]["explicit_negatives"] += int(
                    row["explicit_negative"] is True
                )
                for index in row["class_ids"]:
                    class_rows[index][f"{split}_images"] += 1
                for label in labels:
                    class_rows[label["class_index"]][f"{split}_instances"] += 1
                    area = (
                        label["area"]
                        if segmentation
                        else label["width"] * label["height"]
                    )
                    area_bins[0 if area < 0.01 else 1 if area < 0.1 else 2][split] += 1
        except (TrainingDatasetError, KeyError, ValueError, TypeError) as exc:
            row["issue"] = (
                exc.code
                if isinstance(exc, TrainingDatasetError)
                else "frozen_labels_unavailable"
            )
            unknown_labels += 1
            if split:
                splits[split]["unknown_labels"] += 1
        if row["sha256"]:
            content = contents.setdefault(
                row["sha256"],
                {
                    "semantic_label_digest": row["semantic_label_digest"],
                    "label_sha256": row["label_sha256"],
                    "splits": [],
                    "task_ids": [],
                    "multiplicity": 0,
                },
            )
            if (
                content["semantic_label_digest"] != row["semantic_label_digest"]
                or content["label_sha256"] != row["label_sha256"]
            ):
                content["semantic_label_digest"] = None
                observations["conflicting_content_labels"] += 1
            content["task_ids"].append(task_id)
            content["multiplicity"] += 1
            if split and split not in content["splits"]:
                content["splits"].append(split)
        else:
            unknown_contents += 1
        samples.append(row)
    recorded = bool(
        preparation_report
        and preparation_report.get("schema") == 1
        and preparation_report.get("snapshot_digest") == snapshot_digest
    )
    if recorded:
        for excluded in preparation_report.get("excluded", []):
            if str(excluded["task_id"]) in seen:
                continue
            row = empty_sample(excluded["task_id"], included=False)
            row["exclusion_reason"] = str(excluded["code"])
            samples.append(row)
            seen.add(str(excluded["task_id"]))
    unpackaged = 0
    for task_id in frozen.keys() - seen:
        row = empty_sample(task_id, included=False)
        row["exclusion_reason"] = (
            "package_deduplicated" if snapshot.get("deduplicate") else "not_in_package"
        )
        samples.append(row)
        unpackaged += 1
    try:
        semantic = semantic_partitions(manifest)["validation_digest"]
    except (TrainingDatasetError, KeyError, ValueError, TypeError):
        semantic = None
    validation_members = [m for m in members if m.get("split") == "val"]
    multiset = Counter(
        (m.get("sha256"), m.get("label_sha256")) for m in validation_members
    )
    multiset_digest = (
        content_digest(
            {
                "classes": classes,
                "samples": [
                    [sha, label, n] for (sha, label), n in sorted(multiset.items())
                ],
                **({"data_contract": contract} if segmentation else {}),
            }
        )
        if all(valid_hash(sha) and valid_hash(label) for sha, label in multiset)
        else None
    )
    observations.update(
        {
            "classes_absent_from_train": sum(not c["train_images"] for c in class_rows),
            "classes_absent_from_validation": sum(
                not c["val_images"] for c in class_rows
            ),
            "unknown_labels": unknown_labels,
            "unknown_dimensions": unknown_dimensions,
            "unknown_contents": unknown_contents,
        }
    )
    known = len(members) - unknown_labels
    status = (
        "unavailable"
        if members and not known
        else "partial"
        if unknown_labels
        or unknown_dimensions
        or unknown_contents
        or observations["unknown_split"]
        or observations["conflicting_content_labels"]
        else "complete"
    )
    overview = {
        "status": status,
        "task": contract["task"] if contract else None,
        "counts": {
            "images": len(members),
            "instances": sum((r["instances"] or 0) for r in samples if r["included"]),
            "known_label_images": known,
            "unique_contents": len(contents),
            "duplicate_images": len(members) - unknown_contents - len(contents),
            "negatives": sum(
                r["is_negative"] is True for r in samples if r["included"]
            ),
            "explicit_negatives": sum(
                r["explicit_negative"] is True for r in samples if r["included"]
            ),
            "negative_confirmation_unknown": sum(
                r["is_negative"] is True and r["explicit_negative"] is None
                for r in samples
                if r["included"]
            ),
            "frozen_unpacked_images": unpackaged,
        },
        "splits": splits,
        "classes_total": len(classes),
        "dimensions": {
            "known": len(widths),
            "unknown": unknown_dimensions,
            "min_width": min(widths) if widths else None,
            "max_width": max(widths) if widths else None,
            "min_height": min(heights) if heights else None,
            "max_height": max(heights) if heights else None,
        },
        "dimension_bins": dimension_bins,
        "box_area_bins": area_bins,
        "preparation": {
            "recorded": recorded,
            "summary": preparation_report.get("summary") if recorded else None,
            "issues": len(preparation_report.get("issues", [])) if recorded else 0,
        },
        "observations": dict(observations),
        "validation": {
            "manifest_digest": manifest.get("validation_digest"),
            "semantic_digest": semantic,
            "evaluation_multiset_digest": multiset_digest,
            "ordered_classes_digest": content_digest(classes),
        },
    }
    return {
        "overview": overview,
        "classes": class_rows,
        "contents": contents,
        "history": {
            key.removeprefix("content:"): value
            for key, value in manifest.get("split_history", {}).items()
            if key.startswith("content:")
        },
        "samples": samples,
    }


def compare(baseline: dict, target: dict) -> dict:
    if baseline["overview"].get("task", DETECTION) != target["overview"].get(
        "task", DETECTION
    ):
        raise TrainingDatasetError(
            "task_mismatch",
            "不同训练任务的数据版本不能作为同类数据比较",
            status_code=422,
        )
    before, after = baseline["contents"], target["contents"]
    common = before.keys() & after.keys()
    changes = {key: set() for key in CHANGES}
    changes["added"], changes["removed"] = (
        after.keys() - before.keys(),
        before.keys() - after.keys(),
    )
    changes["reintroduced"] = changes["added"] & baseline["history"].keys()
    for sha in common:
        a, b = before[sha], after[sha]
        if a["semantic_label_digest"] is None or b["semantic_label_digest"] is None:
            changes["unknown"].add(sha)
        elif a["semantic_label_digest"] != b["semantic_label_digest"]:
            changes["labels_changed"].add(sha)
        if set(a["splits"]) != set(b["splits"]):
            changes["split_changed"].add(sha)
        if a["multiplicity"] != b["multiplicity"]:
            changes["multiplicity_changed"].add(sha)
        if set(a["task_ids"]) != set(b["task_ids"]):
            changes["task_ids_changed"].add(sha)
    changes["unchanged"] = (
        common
        - changes["labels_changed"]
        - changes["split_changed"]
        - changes["multiplicity_changed"]
        - changes["unknown"]
    )
    a, b = (
        [c["name"] for c in baseline["classes"]],
        [c["name"] for c in target["classes"]],
    )
    av, bv = baseline["overview"]["validation"], target["overview"]["validation"]

    def differs(key):
        return (
            av[key] != bv[key] if av[key] is not None and bv[key] is not None else None
        )

    return {
        "status": "unavailable"
        if "unavailable"
        in (baseline["overview"]["status"], target["overview"]["status"])
        else "partial"
        if changes["unknown"]
        or baseline["overview"]["status"] != "complete"
        or target["overview"]["status"] != "complete"
        else "complete",
        "counts": {key: len(value) for key, value in changes.items()},
        "classes_added": len(set(b) - set(a)),
        "classes_removed": len(set(a) - set(b)),
        "class_order_changed": set(a) == set(b) and a != b,
        "class_encoding_changed": a != b,
        "semantic_validation_changed": differs("semantic_digest"),
        "evaluation_benchmark_changed": differs("evaluation_multiset_digest"),
        "baseline_validation": av,
        "target_validation": bv,
        "changes": changes,
    }


async def ensure_analysis(db, project_id, version_id):
    statement = (
        select(TrainingDatasetVersion)
        .options(
            defer(TrainingDatasetVersion.snapshot),
            defer(TrainingDatasetVersion.manifest),
            defer(TrainingDatasetVersion.preparation_report),
        )
        .where(
            TrainingDatasetVersion.id == version_id,
            TrainingDatasetVersion.project_id == project_id,
        )
    )
    version = await db.scalar(statement)
    if not version:
        raise HTTPException(status_code=404, detail="数据版本不存在或不属于当前项目")
    if version.status != "ready":
        raise HTTPException(
            status_code=409, detail="数据版本尚未就绪；分析不会改变构建状态"
        )
    manifest_digest = await db.scalar(
        select(TrainingDatasetVersion.manifest["content_digest"].astext).where(
            TrainingDatasetVersion.id == version.id
        )
    )
    identity = {
        "schema": ANALYSIS_SCHEMA,
        "snapshot": version.snapshot_digest,
        "manifest": manifest_digest,
        "package": version.package_sha256,
    }
    fingerprint = content_digest(identity)
    cached = await db.get(TrainingDatasetAnalysis, version.id)
    if cached and cached.input_digest == fingerprint:
        return version, cached, manifest_digest or ""
    # Serialize the one-time metadata build with deletion and other cache creators.
    version = await db.scalar(
        select(TrainingDatasetVersion)
        .where(
            TrainingDatasetVersion.id == version_id,
            TrainingDatasetVersion.project_id == project_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not version:
        raise HTTPException(status_code=404, detail="数据版本已删除")
    cached = await db.get(TrainingDatasetAnalysis, version.id, populate_existing=True)
    current_manifest_digest = version.manifest.get("content_digest")
    fingerprint = content_digest(
        {
            "schema": ANALYSIS_SCHEMA,
            "snapshot": version.snapshot_digest,
            "manifest": current_manifest_digest,
            "package": version.package_sha256,
        }
    )
    if cached and cached.input_digest == fingerprint:
        return version, cached, current_manifest_digest or ""
    if version.status != "ready":
        raise HTTPException(status_code=409, detail="数据版本状态已变化")
    if content_digest(version.snapshot) != version.snapshot_digest:
        raise HTTPException(
            status_code=409, detail="冻结快照校验失败；原有下载入口仍独立可用"
        )
    if (
        current_manifest_digest
        and content_digest(
            {k: v for k, v in version.manifest.items() if k != "content_digest"}
        )
        != current_manifest_digest
    ):
        raise HTTPException(
            status_code=409, detail="冻结清单校验失败；无法生成可信分析"
        )
    try:
        result = await asyncio.to_thread(
            derive,
            version.snapshot,
            version.manifest,
            version.preparation_report,
            version.snapshot_digest,
        )
    except (TrainingDatasetError, ValueError, KeyError, TypeError) as exc:
        message = (
            str(exc)
            if isinstance(exc, TrainingDatasetError)
            else "冻结元数据不完整，分析不可用"
        )
        raise HTTPException(status_code=409, detail=message) from exc
    if cached:
        await db.execute(
            delete(TrainingDatasetAnalysis)
            .where(TrainingDatasetAnalysis.version_id == version.id)
            .execution_options(synchronize_session=False)
        )
        db.expunge(cached)
    rows = result.pop("samples")
    cached = TrainingDatasetAnalysis(
        version_id=version.id, input_digest=fingerprint, summary=result
    )
    db.add(cached)
    await db.flush()
    for offset in range(0, len(rows), 300):
        await db.execute(
            insert(TrainingDatasetAnalyzedSample),
            [{**row, "version_id": version.id} for row in rows[offset : offset + 300]],
        )
    await db.commit()
    return version, cached, current_manifest_digest or ""


async def media_observations(db, version_id):
    rows = (
        await db.execute(
            select(
                TrainingDatasetAnalyzedSample.media_state,
                func.count(),
                func.max(TrainingDatasetAnalyzedSample.media_checked_at),
            )
            .where(
                TrainingDatasetAnalyzedSample.version_id == version_id,
                TrainingDatasetAnalyzedSample.included.is_(True),
            )
            .group_by(TrainingDatasetAnalyzedSample.media_state)
        )
    ).all()
    result = {"verified": 0, "unavailable": 0, "unchecked": 0, "last_checked_at": None}
    for state, count, checked_at in rows:
        result[state] = count
        if checked_at and (
            not result["last_checked_at"] or checked_at > result["last_checked_at"]
        ):
            result["last_checked_at"] = checked_at
    return result


def read_media(project_id, version, sample, *, preview=False):
    """Verify the complete retained object before sending any byte to the caller."""
    key = f"projects/{project_id}/datasets/{version.id}/media/{sample.task_id}"
    temporary = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    body = None
    try:
        head = storage.client.head_object(Bucket=storage.training_bucket, Key=key)
        if (
            head.get("Metadata", {}).get("snapshot-digest") != version.snapshot_digest
            or head.get("Metadata", {}).get("sha256") != sample.sha256
            or head.get("ContentLength") != sample.size_bytes
            or not sample.sha256
            or not sample.size_bytes
            or sample.size_bytes > MAX_IMAGE_BYTES
        ):
            raise TrainingDatasetError(
                "frozen_media_identity", "冻结图片身份或大小不匹配", status_code=409
            )
        response = storage.client.get_object(
            Bucket=storage.training_bucket, Key=key, IfMatch=head["ETag"]
        )
        body = response["Body"]
        digest, size = hashlib.sha256(), 0
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            size += len(chunk)
            if size > sample.size_bytes or size > MAX_IMAGE_BYTES:
                raise TrainingDatasetError(
                    "frozen_media_size", "冻结图片大小校验失败", status_code=409
                )
            digest.update(chunk)
            temporary.write(chunk)
        if size != sample.size_bytes or digest.hexdigest() != sample.sha256:
            raise TrainingDatasetError(
                "frozen_media_digest", "冻结图片字节校验失败", status_code=409
            )
        temporary.seek(0)
        content_type = head.get("ContentType") or "application/octet-stream"
        if preview:
            preview_file = tempfile.SpooledTemporaryFile(
                max_size=8 * 1024 * 1024, mode="w+b"
            )
            try:
                with (
                    Image.open(temporary) as original,
                    ImageOps.exif_transpose(original) as image,
                ):
                    if (
                        sample.width
                        and sample.height
                        and image.size != (sample.width, sample.height)
                    ):
                        raise ValueError("Frozen dimensions differ")
                    image.thumbnail((1600, 1600))
                    if image.mode not in {"RGB", "RGBA"}:
                        image = image.convert("RGB")
                    image.save(preview_file, format="PNG")
            except Exception as exc:
                preview_file.close()
                raise TrainingDatasetError(
                    "frozen_preview_unavailable",
                    "冻结图片通过字节校验，但预览无法解码；原始媒体仍可读取",
                    status_code=409,
                ) from exc
            temporary.close()
            temporary = preview_file
            temporary.seek(0)
            content_type = "image/png"
        return temporary, content_type
    except ClientError as exc:
        temporary.close()
        raise TrainingDatasetError(
            "frozen_media_unavailable",
            "冻结图片已缺失或暂不可用，未替换为当前源图",
            status_code=404,
        ) from exc
    except BotoCoreError as exc:
        temporary.close()
        raise TrainingDatasetError(
            "frozen_media_unavailable",
            "冻结图片存储暂不可用，请稍后重试",
            status_code=503,
        ) from exc
    except BaseException:
        temporary.close()
        raise
    finally:
        if body is not None:
            body.close()


def stream_media(source):
    try:
        while chunk := source.read(1024 * 1024):
            yield chunk
    finally:
        source.close()
