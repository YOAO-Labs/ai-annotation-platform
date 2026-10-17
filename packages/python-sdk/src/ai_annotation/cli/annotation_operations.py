"""Commands for versioned annotation transforms and prediction decisions."""

from __future__ import annotations

from pathlib import Path

import typer

from ai_annotation.annotation_operation_models import (
    AiMaskAcceptRequest,
    AnnotationConversionDryRunRequest,
    AnnotationConversionExecuteRequest,
    AnnotationSliceRestoreRequest,
    InterpolateRangeRequest,
    MaskMutationCommitRequest,
    PolygonSliceCommitRequest,
    PredictionAcceptOptions,
    PropagateBatchRequest,
    PropagateRequest,
    SecondaryInferenceRequest,
)
from ai_annotation.cli._output import (
    cli_errors,
    confirm_destructive,
    get_client,
    print_learning_result,
    read_json_body,
)

app = typer.Typer(
    help="执行标注转换、跨任务传播、Mask 操作和候选决策。",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("conversion-dry-run")
def conversion_dry_run(
    task_id: str = typer.Argument(..., help="来源任务 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """预览转换损失并取得约十分钟有效的 plan token。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, AnnotationConversionDryRunRequest)
        with get_client(json_output) as client:
            result = client.annotation_operations.conversion_dry_run(task_id, request)
    print_learning_result(result, json_output)


@app.command("conversion-execute")
def conversion_execute(
    task_id: str = typer.Argument(..., help="来源任务 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认执行转换"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """执行 dry-run 计划；replace 和 lossy 确认字段由请求文件明确给出。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, AnnotationConversionExecuteRequest)
        confirm_destructive("确认执行标注转换?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.conversion_execute(task_id, request)
    print_learning_result(result, json_output)


@app.command("interpolate-range")
def interpolate_range(
    from_task_id: str = typer.Argument(..., help="区间起点 task ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认生成中间帧标注"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """沿 track_id 在同 scene 的终点任务之间生成插值标注。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, InterpolateRangeRequest)
        confirm_destructive("确认生成区间插值标注?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.interpolate_range(
                from_task_id, request
            )
    print_learning_result(result, json_output)


@app.command("propagate")
def propagate(
    source_task_id: str = typer.Argument(..., help="来源任务 ID"),
    annotation_id: str = typer.Argument(..., help="来源标注 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认复制到目标任务"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """复制一个标注到 request.target_task_id 指定的可见目标任务。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, PropagateRequest)
        confirm_destructive("确认传播标注到目标任务?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.propagate_to_task(
                source_task_id, annotation_id, request
            )
    print_learning_result(result, json_output)


@app.command("propagate-batch")
def propagate_batch(
    source_task_id: str = typer.Argument(..., help="来源任务 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认批量传播"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """批量传播；annotation_ids 为 null 时服务端选择全部 eligible 来源。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, PropagateBatchRequest)
        confirm_destructive("确认批量传播标注?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.propagate_batch(
                source_task_id, request
            )
    print_learning_result(result, json_output)


@app.command("secondary-inference")
def secondary_inference(
    task_id: str = typer.Argument(..., help="任务 ID"),
    annotation_id: str = typer.Argument(..., help="ROI 来源标注 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认运行并写回推理结果"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """同步运行模型；params 和 model_variants 原样传给指定 backend。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, SecondaryInferenceRequest)
        confirm_destructive("确认运行二次推理并写入结果?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.secondary_inference(
                task_id, annotation_id, request
            )
    print_learning_result(result, json_output)


@app.command("mask-mutations-commit")
def mask_mutations_commit(
    task_id: str = typer.Argument(..., help="任务 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认原子提交 Mask 操作"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """原子提交整个 Mask 变更集；不会刷新 fingerprint 或版本冲突。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, MaskMutationCommitRequest)
        confirm_destructive("确认原子提交 Mask 变更集?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.commit_mask_mutations(
                task_id, request
            )
    print_learning_result(result, json_output)


@app.command("polygon-slices-commit")
def polygon_slices_commit(
    task_id: str = typer.Argument(..., help="图片任务 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认切割多边形"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """切割一个已保存 Polygon；保留完整的版本化恢复回执。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, PolygonSliceCommitRequest)
        confirm_destructive("确认切割多边形标注?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.commit_polygon_slice(task_id, request)
    print_learning_result(result, json_output)


@app.command("slice-restore")
def slice_restore(
    task_id: str = typer.Argument(..., help="原切割任务 ID"),
    operation_id: str = typer.Argument(..., help="receipt.slice_operation_id"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认恢复切割状态"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """使用 slice receipt 中的完整版本集恢复 before/after；receipt 有效期 30 天。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, AnnotationSliceRestoreRequest)
        confirm_destructive("确认恢复切割标注状态?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.restore_slice(
                task_id, operation_id, request
            )
    print_learning_result(result, json_output)


@app.command("prediction-accept")
def prediction_accept(
    task_id: str = typer.Argument(..., help="任务 ID"),
    prediction_id: str = typer.Argument(..., help="预测 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 决策选项文件，- 表示 stdin"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认采纳候选"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """采纳候选；shape_index 必须来自 GET predictions 的原始结果项。"""
    with cli_errors(json_output):
        options = read_json_body(request_file, PredictionAcceptOptions)
        confirm_destructive("确认采纳预测候选并创建标注?", yes, json_output)
        with get_client(json_output) as client:
            annotations = client.annotation_operations.accept_prediction(
                task_id,
                prediction_id,
                shape_index=options.shape_index,
                video_segment_id=options.video_segment_id,
                override_class_name=options.override_class_name,
                attribute_overrides=options.attribute_overrides,
            )
    print_learning_result(
        [
            annotation.model_dump(mode="json", by_alias=True)
            for annotation in annotations
        ],
        json_output,
    )


@app.command("prediction-reject")
def prediction_reject(
    task_id: str = typer.Argument(..., help="任务 ID"),
    prediction_id: str = typer.Argument(..., help="预测 ID"),
    shape_index: int | None = typer.Option(
        None,
        "--shape-index",
        min=0,
        help="原始 prediction.result 下标；省略则驳回全部 shape",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="确认驳回候选"),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """软驳回一个原始 shape 下标或整条 prediction。"""
    with cli_errors(json_output):
        confirm_destructive("确认驳回预测候选?", yes, json_output)
        with get_client(json_output) as client:
            client.annotation_operations.reject_prediction(
                task_id, prediction_id, shape_index=shape_index
            )
    print_learning_result(
        {"rejected": True, "prediction_id": prediction_id, "shape_index": shape_index},
        json_output,
    )


@app.command("ai-mask-accept")
def ai_mask_accept(
    task_id: str = typer.Argument(..., help="任务 ID"),
    request_file: Path = typer.Option(
        ..., "--request", help="严格 JSON 请求文件，- 表示 stdin"
    ),
    if_match: int | None = typer.Option(
        None, "--if-match", min=1, help="refine 源标注的预期版本"
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="确认接受 receipt-backed Mask 候选"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出裸 JSON"),
) -> None:
    """接受原生 Mask 候选；refine 时 --if-match 必须与 target.source_version 一致。"""
    with cli_errors(json_output):
        request = read_json_body(request_file, AiMaskAcceptRequest)
        confirm_destructive("确认接受 Mask 候选并写入内容对象?", yes, json_output)
        with get_client(json_output) as client:
            result = client.annotation_operations.accept_ai_mask_candidate(
                task_id, request, if_match=if_match
            )
    print_learning_result(result, json_output)


__all__ = ["app"]
