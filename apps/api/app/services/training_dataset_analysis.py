"""Analyze only retained, frozen package members; never consult live annotations."""

from __future__ import annotations

from aap_task_contract import normalize_task

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
from app.services import training_dataset_storage, storage_access
from app.services.training_dataset_bridge import (
    TrainingDatasetError,
    content_digest,
    contract_of,
    semantic_partitions,
)
from app.services.training_datasets import MAX_IMAGE_BYTES, MAX_SAMPLES
from training_contract.dataset import (
    SEGMENTATION,
    is_instance_rle,
    instance_rle_evidence,
    INSTANCE_RLE_LIMITS,
)
from training_contract.lineage import validate_source_lineage
from app.services.training_dataset_sources import frozen_source_evidence
from app.services.training_dataset_insight_adapters import (
    adapter_cache_identity,
    adapter_for_profile,
    contract_profile,
    unsupported_capabilities,
)

ANALYSIS_SCHEMA = 7
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
    "source_relation_changed",
    "source_evidence_updated",
    "source_unknown",
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


def comparison_validation_identity(classes, profile, partitions, multiset):
    """Normalize only derived comparison identity, never frozen training digests."""
    if profile is None:
        return None
    known = partitions is not None and all(
        valid_hash(sha) and valid_hash(label) for sha, label in multiset
    )
    basis = {"contract_profile": profile, "classes": classes}
    return {
        "schema_version": 1,
        "contract_profile_digest": content_digest(profile),
        "semantic_digest": content_digest({**basis, "samples": partitions["val"]})
        if known
        else None,
        "evaluation_multiset_digest": content_digest(
            {
                **basis,
                "samples": [
                    [sha, label, count]
                    for (sha, label), count in sorted(multiset.items())
                ],
            }
        )
        if known
        else None,
        "ordered_classes_digest": content_digest(classes),
    }


def derive(
    snapshot: dict,
    manifest: dict,
    preparation_report: dict | None,
    snapshot_digest: str,
) -> dict:
    validate_source_lineage(snapshot)
    validate_source_lineage(manifest)
    if snapshot.get("source_lineage_contract") != manifest.get(
        "source_lineage_contract"
    ):
        raise TrainingDatasetError("frozen_source_mismatch", "冻结来源合同与清单不一致")
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
    if "managed_original_contract" in snapshot and len(frozen) != len(
        snapshot.get("samples", [])
    ):
        raise TrainingDatasetError("frozen_source_mismatch", "托管冻结来源成员身份重复")
    try:
        contract = contract_of(manifest)
        profile = contract_profile(manifest)
        adapter = adapter_for_profile(profile)
        unavailable_reason = None
    except TrainingDatasetError as exc:
        contract, profile, adapter = None, None, None
        unavailable_reason = str(exc)
    try:
        snapshot_profile = contract_profile(snapshot)
    except TrainingDatasetError:
        snapshot_profile = None
    segmentation = bool(contract and normalize_task(contract["task"]) == SEGMENTATION)
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
        if member.get("source_lineage") != (source or {}).get("source_lineage"):
            raise TrainingDatasetError(
                "frozen_source_mismatch", "冻结来源证据与清单不一致"
            )
        try:
            if (
                not source
                or "annotations" not in source
                or snapshot.get("classes") != classes
                or contract is None
                or snapshot_profile != profile
            ):
                raise TrainingDatasetError(
                    "frozen_labels_unavailable", "冻结标注或类别映射未保留"
                )
            if is_instance_rle(contract):
                instances, labels = adapter.mask_labels(
                    source["annotations"], classes, member
                )
                text = None
                from app.services.training_dataset_bridge import instance_rle_semantic

                if member.get("semantic_label_sha256") != instance_rle_semantic(
                    instances
                ):
                    raise TrainingDatasetError(
                        "frozen_labels_mismatch", "掩码像素语义摘要不一致"
                    )
            else:
                text, instances, labels = adapter.labels(
                    source["annotations"], classes, contract
                )
            if segmentation and member.get("instances") != instances:
                raise TrainingDatasetError(
                    "frozen_instances_mismatch", "冻结实例来源与训练包不一致"
                )
            if (
                not row["label_sha256"]
                or text is not None
                and hashlib.sha256(text.encode()).hexdigest() != row["label_sha256"]
            ):
                raise TrainingDatasetError(
                    "frozen_labels_mismatch", "冻结标签与训练包摘要不一致"
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
                semantic_label_digest=adapter.semantic_fingerprint(labels),
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
                    "unknown_split_members": 0,
                    "task_ids": [],
                    "multiplicity": 0,
                    "source_relations": [],
                    "source_evidence": [],
                    "unknown_source_members": 0,
                },
            )
            lineage = frozen_source_evidence(
                snapshot, manifest, member, source, snapshot_digest
            )
            if lineage["status"] != "recorded":
                content["unknown_source_members"] += 1
            else:
                evidence = lineage["evidence"] or lineage["managed_evidence"]
                content["source_relations"].append(
                    content_digest(
                        {
                            "kind": lineage["kind"],
                            **{
                                key: evidence[key]
                                for key in (
                                    ("owner_kind", "content_id", "location_id")
                                    if lineage["kind"] == "managed_original"
                                    else ("source_id", "location_id", "path_digest")
                                )
                            },
                        }
                    )
                )
                content["source_evidence"].append(content_digest(evidence))
            if (
                content["semantic_label_digest"] != row["semantic_label_digest"]
                or content["label_sha256"] != row["label_sha256"]
            ):
                content["semantic_label_digest"] = None
                observations["conflicting_content_labels"] += 1
            content["task_ids"].append(task_id)
            content["multiplicity"] += 1
            if split is None:
                content["unknown_split_members"] += 1
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
        partitions = semantic_partitions(manifest)
        semantic = partitions["validation_digest"]
    except (TrainingDatasetError, KeyError, ValueError, TypeError):
        partitions = None
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
            "classes_with_no_known_train_instances": sum(
                not c["train_instances"] for c in class_rows
            ),
            "classes_with_no_known_validation_instances": sum(
                not c["val_instances"] for c in class_rows
            ),
            "unknown_labels": unknown_labels,
            "unknown_dimensions": unknown_dimensions,
            "unknown_contents": unknown_contents,
        }
    )
    for split in ("train", "val"):
        if not splits[split]["unknown_labels"] and not observations["unknown_split"]:
            observations[
                f"classes_absent_from_{'validation' if split == 'val' else 'train'}"
            ] = sum(not c[f"{split}_images"] for c in class_rows)
    known = len(members) - unknown_labels
    included_rows = [row for row in samples if row["included"]]
    known_instances = sum(row["instances"] or 0 for row in included_rows)
    observed_classes = len(
        {index for row in included_rows for index in row["class_ids"]}
    )
    observations["observed_classes"] = observed_classes
    geometry_unknown = (
        adapter.geometry_unknown_members(included_rows) if adapter else len(members)
    )
    observations["geometry_unknown_members"] = geometry_unknown
    status = (
        "unavailable"
        if adapter is None or members and not known
        else "partial"
        if unknown_labels
        or geometry_unknown
        or unknown_dimensions
        or unknown_contents
        or observations["unknown_split"]
        or observations["conflicting_content_labels"]
        else "complete"
    )
    overview = {
        "status": status,
        "task": profile["task"] if profile else None,
        "contract_profile": profile,
        "adapter": {"id": adapter.id, "revision": adapter.revision}
        if adapter
        else None,
        "capabilities": adapter.capabilities(snapshot)
        if adapter
        else unsupported_capabilities(unavailable_reason),
        "evidence": {
            "analyzed_members": known,
            "unknown_label_members": unknown_labels,
            "known_instances": known_instances,
            "geometry_unknown_members": geometry_unknown,
        },
        "task_statistics": adapter.statistics(included_rows) if adapter else None,
        "counts": {
            "images": len(members),
            "instances": known_instances,
            "observed_classes": observed_classes,
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
            "raw_contract_digest": content_digest(contract) if contract else None,
            "comparison_identity": comparison_validation_identity(
                classes, profile, partitions, multiset
            ),
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
    before_profile, after_profile = (
        value["overview"].get("contract_profile") for value in (baseline, target)
    )
    if before_profile is None or after_profile is None:
        raise TrainingDatasetError(
            "unsupported_insight_contract",
            "数据契约没有足够证据支持语义比较",
            status_code=422,
        )
    adapter_for_profile(before_profile).assert_comparable(
        adapter_for_profile(after_profile)
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
        if a.get("unknown_source_members") != 0 or b.get("unknown_source_members") != 0:
            changes["source_unknown"].add(sha)
        elif sorted(a["source_relations"]) != sorted(b["source_relations"]):
            changes["source_relation_changed"].add(sha)
        elif sorted(a["source_evidence"]) != sorted(b["source_evidence"]):
            changes["source_evidence_updated"].add(sha)
        if a["semantic_label_digest"] is None or b["semantic_label_digest"] is None:
            changes["unknown"].add(sha)
        elif a["semantic_label_digest"] != b["semantic_label_digest"]:
            changes["labels_changed"].add(sha)
        # Partial split sets cannot establish either a change or sameness. An
        # older derived record without this evidence is unknown, never zero.
        if a.get("unknown_split_members") != 0 or b.get("unknown_split_members") != 0:
            changes["unknown"].add(sha)
        elif set(a["splits"]) != set(b["splits"]):
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

    def differs(left, right, key):
        return (
            left[key] != right[key]
            if left
            and right
            and left.get(key) is not None
            and right.get(key) is not None
            else None
        )

    normalized_before, normalized_after = (
        value.get("comparison_identity") for value in (av, bv)
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
        "count_unit": "unique_contents",
        "source_comparison_status": "unavailable"
        if not common
        else "partial"
        if changes["source_unknown"]
        else "complete",
        "baseline_members": baseline["overview"]["counts"]["images"],
        "target_members": target["overview"]["counts"]["images"],
        "overlapping_changes": True,
        "semantic_unchanged_label": "训练语义未变化",
        "classes_added": len(set(b) - set(a)),
        "classes_removed": len(set(a) - set(b)),
        "class_order_changed": set(a) == set(b) and a != b,
        "class_encoding_changed": a != b,
        "semantic_validation_changed": differs(
            normalized_before, normalized_after, "semantic_digest"
        ),
        "evaluation_benchmark_changed": differs(
            normalized_before, normalized_after, "evaluation_multiset_digest"
        ),
        "validation_comparison_basis": "canonical-profile-validation-1",
        "raw_contract_changed": differs(av, bv, "raw_contract_digest"),
        "baseline_validation": av,
        "target_validation": bv,
        "changes": changes,
    }


def analysis_input_digest(
    snapshot_digest: str, manifest: dict, package_digest: str | None
) -> str:
    return content_digest(
        {
            "schema": ANALYSIS_SCHEMA,
            "snapshot": snapshot_digest,
            "manifest": manifest.get("content_digest"),
            "package": package_digest,
            "adapter": adapter_cache_identity(manifest),
        }
    )


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
    manifest_identity = await db.scalar(
        select(
            func.jsonb_build_object(
                *[
                    part
                    for key in (
                        "content_digest",
                        "schema",
                        "task",
                        "media_type",
                        "format",
                        "geometry",
                        "bridge",
                        "source_lineage_contract",
                    )
                    for part in (
                        key,
                        func.jsonb_build_object(
                            "present",
                            TrainingDatasetVersion.manifest.has_key(key),
                            "value",
                            TrainingDatasetVersion.manifest[key],
                        ),
                    )
                ]
            )
        ).where(TrainingDatasetVersion.id == version.id)
    )
    if manifest_identity is None:
        raise HTTPException(status_code=404, detail="数据版本已删除")
    # Distinguish historical absence from an explicit invalid null. Dispatch and
    # cache identity must be identical on the compact projection and full record.
    manifest_identity = {
        key: entry["value"]
        for key, entry in manifest_identity.items()
        if entry["present"]
    }
    manifest_digest = manifest_identity.get("content_digest")
    fingerprint = analysis_input_digest(
        version.snapshot_digest, manifest_identity, version.package_sha256
    )
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
    fingerprint = analysis_input_digest(
        version.snapshot_digest, version.manifest, version.package_sha256
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
    media_observations = {}
    if cached:
        observed = (
            await db.execute(
                select(
                    TrainingDatasetAnalyzedSample.task_id,
                    TrainingDatasetAnalyzedSample.sha256,
                    TrainingDatasetAnalyzedSample.size_bytes,
                    TrainingDatasetAnalyzedSample.included,
                    TrainingDatasetAnalyzedSample.media_state,
                    TrainingDatasetAnalyzedSample.media_checked_at,
                ).where(
                    TrainingDatasetAnalyzedSample.version_id == version.id,
                    TrainingDatasetAnalyzedSample.media_state != "unchecked",
                )
            )
        ).all()
        media_observations = {tuple(row[:4]): tuple(row[4:]) for row in observed}
        await db.execute(
            delete(TrainingDatasetAnalysis)
            .where(TrainingDatasetAnalysis.version_id == version.id)
            .execution_options(synchronize_session=False)
        )
        db.expunge(cached)
    rows = result.pop("samples")
    for row in rows:
        identity = tuple(
            row[key] for key in ("task_id", "sha256", "size_bytes", "included")
        )
        if identity in media_observations:
            row["media_state"], row["media_checked_at"] = media_observations[identity]
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


def read_media(project_id, version, sample, *, preview=False, transport=None):
    """Verify the complete retained object before sending any byte to the caller."""
    owner = training_dataset_storage.resolve(version)
    if transport is None and owner.key_prefix:
        training_dataset_storage._reject("training_storage_binding_unproven")
    destination = transport or storage
    if str(project_id) != str(version.project_id):
        raise TrainingDatasetError(
            "training_storage_invalid_key", "冻结图片不属于该项目", status_code=409
        )
    key = owner.key(f"media/{sample.task_id}")
    temporary = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    body = None
    try:
        head = destination.client.head_object(Bucket=owner.bucket, Key=key)
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
        response = destination.client.get_object(
            Bucket=owner.bucket, Key=key, IfMatch=head["ETag"]
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
            storage_access.close_registered(body)


def stream_media(source):
    try:
        while chunk := source.read(1024 * 1024):
            yield chunk
    finally:
        source.close()


def read_instance_masks(project_id, version, sample, annotation_ids, *, transport=None):
    """Read and verify requested instances from one independent frozen object."""
    if not sample.included or not sample.label_sha256:
        raise TrainingDatasetError(
            "frozen_mask_unavailable", "冻结掩码不可用", status_code=404
        )
    requested = {str(identifier) for identifier in annotation_ids}
    if not requested:
        return {}
    owner = training_dataset_storage.resolve(version)
    if transport is None and owner.key_prefix:
        training_dataset_storage._reject("training_storage_binding_unproven")
    destination = transport or storage
    if str(project_id) != str(version.project_id):
        raise TrainingDatasetError(
            "training_storage_invalid_key", "冻结掩码不属于该项目", status_code=409
        )
    key = owner.key(f"masks/{sample.task_id}.json")
    result = {}
    try:
        response = destination.client.get_object(Bucket=owner.bucket, Key=key)
        try:
            data = response["Body"].read(
                INSTANCE_RLE_LIMITS["max_sample_label_bytes"] + 1
            )
        finally:
            storage_access.close_registered(response["Body"])
        if len(data) > INSTANCE_RLE_LIMITS["max_sample_label_bytes"]:
            raise ValueError("frozen mask object exceeds budget")
        if hashlib.sha256(data).hexdigest() != sample.label_sha256:
            raise ValueError("frozen mask object digest mismatch")
        import json

        payload = json.loads(data)
        for instance in payload["instances"]:
            identifier = instance["annotation_id"]
            if identifier in requested:
                if identifier in result:
                    raise ValueError("duplicate frozen mask identity")
                evidence = instance_rle_evidence(instance["rle"])
                if any(instance[key] != value for key, value in evidence.items()):
                    raise ValueError("frozen mask evidence mismatch")
                result[identifier] = {
                    "annotation_id": identifier,
                    "annotation_version": instance["annotation_version"],
                    "pixel_sha256": evidence["pixel_sha256"],
                    "rle": instance["rle"],
                }
    except (BotoCoreError, ClientError, ValueError, KeyError, TypeError) as exc:
        raise TrainingDatasetError(
            "frozen_mask_unavailable", "冻结掩码不可用或校验失败", status_code=409
        ) from exc
    if set(result) != requested:
        raise TrainingDatasetError(
            "frozen_mask_not_found", "实例不属于该冻结样本", status_code=404
        )
    return result


def read_instance_mask(project_id, version, sample, annotation_id, *, transport=None):
    """Read one frozen mask from the version-owned independent training object."""
    return read_instance_masks(
        project_id, version, sample, [annotation_id], transport=transport
    )[str(annotation_id)]
