"""Current unlabeled sources and immutable historical exclusion membership."""

from __future__ import annotations

from sqlalchemy import String, cast, func, select

from app.db.models.sample_evaluation import SampleEvaluationRun as Evaluation
from app.db.models.training_dataset import TrainingDatasetVersion as Version
from app.services import active_learning_engine as strategy
from app.services import active_learning_originals as originals
from app.services import sample_evaluation_engine as detection
from app.services.labeling_active_learning_source_contract import (
    CandidateSourceError,
    CandidateSourcesRead,
    LabelingActiveLearningSources,
)
from app.services.managed_inference import qualification, reject
from app.services.training_authority_contract import TrainingAuthorityPort
from app.services.training_dataset_bridge import (
    content_digest,
    sample_identities,
    split_history,
)

MAX_TOTAL_RAW = 128 * 1024 * 1024
MAX_HISTORICAL_BYTES = 16 * 1024 * 1024
MAX_HISTORICAL_SOURCES = 256
SCOPES = originals.SCOPES


async def exclusions(db, project_id, *, lock=False):
    """Fail closed at the history budget; never silently drop old membership."""
    clauses = [Version.project_id == project_id]
    version_sizes = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        func.coalesce(
                            func.octet_length(cast(Version.snapshot, String)), 0
                        )
                        + func.coalesce(
                            func.octet_length(cast(Version.manifest, String)), 0
                        )
                    ),
                    0,
                ),
            ).where(*clauses)
        )
    ).one()
    evaluation_sizes = (
        await db.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(func.octet_length(cast(Evaluation.spec, String))), 0
                ),
            ).where(
                Evaluation.project_id == project_id,
                Evaluation.spec["role"].astext == "held_out_test",
            )
        )
    ).one()
    if (
        version_sizes[0] + evaluation_sizes[0] > MAX_HISTORICAL_SOURCES
        or version_sizes[1] + evaluation_sizes[1] > MAX_HISTORICAL_BYTES
    ):
        raise reject(
            "history_budget",
            "项目保留历史超过本期有界排除检查范围，未省略任何历史成员",
            422,
        )
    versions_query = select(Version).where(*clauses).order_by(Version.id)
    evals_query = (
        select(Evaluation)
        .where(
            Evaluation.project_id == project_id,
            Evaluation.spec["role"].astext == "held_out_test",
        )
        .order_by(Evaluation.id)
    )
    if lock:
        versions_query = versions_query.with_for_update(read=True)
        evals_query = evals_query.with_for_update(read=True)
    versions = (
        await db.scalars(versions_query.execution_options(populate_existing=True))
    ).all()
    evaluations = (
        await db.scalars(evals_query.execution_options(populate_existing=True))
    ).all()
    identities, refs = set(), []
    for version in versions:
        if content_digest(version.snapshot) != version.snapshot_digest:
            raise reject("history_corrupt", "保留的训练数据快照未通过身份校验")
        manifest = version.manifest or {}
        digest = manifest.get("content_digest")
        membership = split_history(manifest)
        expected = {
            f"task:{sample['task_id']}"
            for sample in version.snapshot.get("samples", [])
        }
        expected.update(
            f"scene:{sample['scene_id']}"
            for sample in version.snapshot.get("samples", [])
            if sample.get("scene_id")
        )
        if (
            not digest
            or manifest.get("source_digest") != version.snapshot_digest
            or not expected <= set(membership)
        ):
            raise reject(
                "history_unready",
                "保留的数据版本尚无覆盖全部成员的内容清单；请先完成构建或移除未使用的失败版本",
            )
        if manifest and (
            not digest
            or content_digest(
                {k: v for k, v in manifest.items() if k != "content_digest"}
            )
            != digest
        ):
            raise reject("history_corrupt", "保留的训练数据清单未通过身份校验")
        for sample in version.snapshot.get("samples", []):
            identities.add(f"task:{sample['task_id']}")
            if sample.get("scene_id"):
                identities.add(f"scene:{sample['scene_id']}")
        if any(value not in {"train", "val"} for value in membership.values()):
            raise reject("history_corrupt", "历史训练与验证成员分区身份无效")
        identities.update(membership)
        for sample in manifest.get("samples", []):
            identities.update(sample_identities(sample))
        refs.append(
            {
                "kind": "dataset",
                "id": str(version.id),
                "digest": content_digest(
                    {
                        "snapshot_digest": version.snapshot_digest,
                        "manifest_digest": digest,
                    }
                ),
            }
        )
    for evaluation in evaluations:
        if (
            content_digest(evaluation.spec) != evaluation.spec_digest
            or evaluation.deletion_requested_at
        ):
            raise reject("history_corrupt", "保留的独立测试规格已变化或正在删除")
        for sample in evaluation.spec["samples"]:
            identities.update(sample_identities(sample))
        refs.append(
            {
                "kind": "evaluation",
                "id": str(evaluation.id),
                "digest": evaluation.spec_digest,
            }
        )
    result = {"identities": sorted(identities), "references": refs}
    return {**result, "digest": content_digest(result)}


async def current_sources(
    db,
    project_id,
    task_ids,
    *,
    candidate_sources: LabelingActiveLearningSources,
):
    """Read the Labeling-owned facts and pin each original input receipt."""
    try:
        snapshots = await candidate_sources.read_candidate_sources(
            CandidateSourcesRead(project_id, tuple(task_ids))
        )
    except CandidateSourceError as exc:
        raise reject(exc.code, str(exc), exc.status_code) from exc
    return [await originals.pin(db, snapshot) for snapshot in snapshots]


async def prepare(
    db,
    project,
    body,
    actor_id,
    api_key_id=None,
    *,
    request,
    candidate_sources: LabelingActiveLearningSources,
    training_authority: TrainingAuthorityPort,
):
    context = await originals.authorize(db, project.id, actor_id, request, api_key_id)
    # External qualification and media IO precede final domain locks.
    ref, profile = await qualification(
        db, project, body.model_version_id, body.deployment_id
    )
    await training_authority.assert_authority(
        project_id=project.id,
        actor_id=actor_id,
        api_key_id=api_key_id,
        required_scopes=SCOPES,
    )
    before = await current_sources(
        db, project.id, body.sample_task_ids, candidate_sources=candidate_sources
    )
    history = await exclusions(db, project.id)
    await db.commit()
    verified, live_sources, total = [], [], 0
    for snapshot in before:
        try:
            _png, media, source = await originals.read_input(db, context, snapshot)
            live_sources.append(source)
        except originals.ContentError:
            raise
        except (ValueError, OSError) as exc:
            if getattr(exc, "storage_operation_unsettled", False):
                raise
            raise reject(
                "source_unavailable",
                "候选图片未通过条件读取、解码、大小或身份检查",
                422,
            ) from exc
        total += media["size_bytes"]
        if strategy.strategy_for(ref) == strategy.MASK_STRATEGY:
            from app.services import sample_evaluation_masks as masks

            if (
                media["width"] * media["height"] > masks.MAX_PIXELS
                or max(media["width"], media["height"]) > masks.MAX_DIMENSION
            ):
                raise reject("source_budget", "候选原图超过分割评估协议的像素预算", 422)
        if total > MAX_TOTAL_RAW:
            raise reject("source_budget", "候选图片总量超过 128 MiB", 422)
        verified.append(
            {
                "task_id": snapshot["task_id"],
                "scene_id": snapshot["scene_id"],
                "snapshot": snapshot,
                **media,
            }
        )
    await training_authority.assert_authority(
        project_id=project.id,
        actor_id=actor_id,
        api_key_id=api_key_id,
        required_scopes=SCOPES,
    )
    after = await current_sources(
        db, project.id, body.sample_task_ids, candidate_sources=candidate_sources
    )
    if before != after or history != await exclusions(db, project.id):
        raise reject("pool_stale", "核验期间候选来源或历史成员已变化，请重新预览")
    for source in live_sources:
        await originals.recheck(db, context, source, commit=False)
    samples, omitted = strategy.grouped_pool(verified, history["identities"])
    samples = strategy.partition(samples, body.seed, body.batch_size)
    if body.max_inference_dispatches < len(samples):
        raise reject("inference_budget", "推理预算至少须覆盖去重后的全部独立候选", 422)
    result = {
        "schema": 1,
        "original_contract": 1,
        "pool": verified,
        "pool_digest": content_digest(verified),
        "samples": samples,
        "omitted": omitted,
        "exclusions": history,
        "model_ref": ref,
        "profile": profile,
        "profile_digest": profile["profile_digest"],
        "strategy": strategy.strategy_for(ref),
        "preprocessing": detection.PREPROCESSING,
        **body.model_dump(
            mode="json",
            include={
                "seed",
                "batch_size",
                "max_inference_dispatches",
                "max_review_tasks",
                "confidence",
                "nms_iou",
            },
        ),
    }
    if strategy.strategy_for(ref) == strategy.MASK_STRATEGY:
        result.update(schema=2, schema_version=2, task="segment")
    await db.commit()
    originals.access.check_clock(context.proof, context.key)
    return {
        **result,
        "preview_digest": content_digest(result),
        "_authority": context,
        "_live_sources": live_sources,
    }
