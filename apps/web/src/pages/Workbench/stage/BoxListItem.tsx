import { CopyableError } from "@/components/ui/CopyableError";
import { useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { Annotation } from "@/types";
import { predictionSourceLabel, type AiBox } from "../state/transforms";
import { classColor, displayClassName } from "./colors";
import type { RasterMaskRecordStatus } from "./shared/useRasterMaskRecords";

function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 4)}...${value.slice(-4)}` : value;
}

function frameRange(frames: number[]): string {
  if (frames.length === 0) return "无帧";
  const min = Math.min(...frames);
  const max = Math.max(...frames);
  return min === max ? `F${min}` : `F${min}-F${max}`;
}

function rectText(
  x: number,
  y: number,
  w: number,
  h: number,
  imageWidth: number | null,
  imageHeight: number | null,
): string {
  if (imageWidth && imageHeight) {
    return `(${Math.round(x * imageWidth)}, ${Math.round(y * imageHeight)}) · ${Math.round(w * imageWidth)}×${Math.round(h * imageHeight)}`;
  }
  return `${pct(w)} × ${pct(h)}`;
}

function annotationToolMeta(
  b: Annotation,
  imageWidth: number | null,
  imageHeight: number | null,
): { label: string; detail: string } {
  const geometry = b.geometry;
  if (!geometry) {
    return {
      label: b.polygon ? "多边形" : "矩形框",
      detail: rectText(b.x, b.y, b.w, b.h, imageWidth, imageHeight),
    };
  }

  if (geometry.type === "bbox") {
    return {
      label: "矩形框",
      detail: rectText(geometry.x, geometry.y, geometry.w, geometry.h, imageWidth, imageHeight),
    };
  }
  if (geometry.type === "video_bbox") {
    return {
      label: "视频矩形框",
      detail: `F${geometry.frame_index} · ${rectText(geometry.x, geometry.y, geometry.w, geometry.h, imageWidth, imageHeight)}`,
    };
  }
  if (geometry.type === "video_track_bbox") {
    const frames = geometry.keyframes.map((kf) => kf.frame_index);
    const outsideFrames = (geometry.outside ?? []).reduce((sum, r) => sum + (r.to - r.from + 1), 0);
    const occluded = geometry.keyframes.filter((kf) => kf.occluded).length;
    return {
      label: "轨迹",
      detail: `${shortId(geometry.track_id)} · ${geometry.keyframes.length} 关键帧 · ${frameRange(frames)}${outsideFrames ? ` · ${outsideFrames} 消失` : ""}${occluded ? ` · ${occluded} 遮挡` : ""}`,
    };
  }
  if (geometry.type === "polygon") {
    return {
      label: "多边形",
      detail: `${geometry.points.length} 点${geometry.holes?.length ? ` · ${geometry.holes.length} 内环` : ""}`,
    };
  }
  if (geometry.type === "rotated_bbox") {
    return {
      label: "旋转框",
      detail: `${rectText(geometry.cx - geometry.w / 2, geometry.cy - geometry.h / 2, geometry.w, geometry.h, imageWidth, imageHeight)} · ${Math.round(geometry.angle)}°`,
    };
  }
  if (geometry.type === "polyline") {
    return { label: "折线", detail: `${geometry.points.length} 点` };
  }
  if (geometry.type === "keypoint") {
    const visible = geometry.points.filter((p) => p.v === 2).length;
    return { label: "关键点", detail: `${visible}/${geometry.points.length} 可见` };
  }
  if (geometry.type === "box_3d") {
    const [l, w, h] = geometry.size;
    return { label: "3D 框", detail: `${l.toFixed(1)}×${w.toFixed(1)}×${h.toFixed(1)} m` };
  }
  if (geometry.type === "point_mask_3d") {
    return { label: "点云掩码", detail: `${geometry.point_indices.length} 点` };
  }
  if (geometry.type === "video_track_polygon") {
    return { label: "轨迹(多边形)", detail: `${geometry.keyframes.length} 关键帧` };
  }
  if (geometry.type === "video_track_polyline") {
    return { label: "轨迹(折线)", detail: `${geometry.keyframes.length} 关键帧` };
  }
  if (geometry.type === "video_track_mask") {
    return { label: "轨迹(栅格掩码)", detail: `${geometry.keyframes.length} 关键帧` };
  }
  if (geometry.type === "video_mask") {
    return {
      label: "栅格掩码",
      detail: `F${geometry.frame_index} · ${geometry.mask.size[1]}×${geometry.mask.size[0]} px · ${geometry.mask.runs} 编码段`,
    };
  }
  if (geometry.type === "video_polygon") {
    return { label: "多边形", detail: `F${geometry.frame_index} · ${geometry.points.length} 点` };
  }
  if (geometry.type === "video_polyline") {
    return { label: "折线", detail: `F${geometry.frame_index} · ${geometry.points.length} 点` };
  }
  if (geometry.type === "video_rotated_bbox") {
    return { label: "旋转框", detail: `F${geometry.frame_index} · ${Math.round(geometry.angle)}°` };
  }
  if (geometry.type === "video_keypoint") {
    const visible = geometry.points.filter((point) => point.v === 2).length;
    return {
      label: "关键点",
      detail: `F${geometry.frame_index} · ${visible}/${geometry.points.length} 可见`,
    };
  }
  if (geometry.type === "raster_mask") {
    return {
      label: "栅格掩码",
      detail: `${geometry.mask.size[1]}×${geometry.mask.size[0]} px · ${geometry.mask.runs} 编码段`,
    };
  }
  if (geometry.type === "multi_polygon") {
    return {
      label: "多连通域",
      detail: `${geometry.polygons.length} 区域 · ${geometry.polygons.reduce((sum: number, p) => sum + p.points.length, 0)} 点`,
    };
  }
  return { label: "未知", detail: "" };
}

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

function getPredictionSource(b: Annotation | AiBox) {
  return "predictionSource" in b ? b.predictionSource : null;
}

// v0.14.9 · doc_layout 版面类别 (后端 class_name). 命中时在候选行展示版面 type badge.
const DOC_LAYOUT_LABELS: Record<string, string> = {
  title: "标题",
  paragraph: "段落",
  table: "表格",
  figure: "图片",
  formula: "公式",
  list: "列表",
  header: "页眉",
  footer: "页脚",
};

// v0.14.9 · 从 AI 候选 attributes 抽 OCR 文本摘要 (单行截断). 无文本返回 null.
function ocrTextSummary(b: Annotation | AiBox): string | null {
  if (!("attributes" in b) || !b.attributes) return null;
  const text = b.attributes.text;
  if (typeof text !== "string" || text.trim() === "") return null;
  const trimmed = text.trim();
  return trimmed.length > 48 ? `${trimmed.slice(0, 48)}…` : trimmed;
}

// v0.18.0 · 二阶段候选属性摘要 (车型 / 颜色等 select). 排除 text(OCR 摘要已单列) 与
// 非原子值. 此处无项目 schema, 只能展示原始存储值 (英文枚举); 中文标签由侧栏属性预览
// (AttributeForm + options) 负责. 仅 AI 候选行展示, 给采纳前一眼可辨的信号.
function attributesSummary(b: Annotation | AiBox): string[] {
  if (!("attributes" in b) || !b.attributes) return [];
  const out: string[] = [];
  for (const [key, val] of Object.entries(b.attributes)) {
    if (key === "text" || key.startsWith("_")) continue;
    if (val == null || val === "" || typeof val === "object") continue;
    out.push(String(val));
  }
  return out;
}

// v0.14.9 · 命中 doc_layout 版面类别时返回中文 badge 文案, 否则 null.
function docLayoutBadge(b: Annotation | AiBox): string | null {
  const key = b.cls?.toLowerCase?.();
  return key && key in DOC_LAYOUT_LABELS ? DOC_LAYOUT_LABELS[key] : null;
}

interface BoxListItemProps {
  b: Annotation | AiBox;
  isAi?: boolean;
  selected: boolean;
  /** dimmed 时整行半透明 + "已被覆盖" tag（IoU 去重）。 */
  dimmed?: boolean;
  /** 像素维度。null 时显示 — 占位（dataset_items 尚未回填 width/height）。 */
  imageWidth: number | null;
  imageHeight: number | null;
  onSelect: (e?: { shiftKey?: boolean }) => void;
  onAccept?: () => void;
  onReject?: () => void;
  /** v0.10.8 · I11 · 精修：仅 AI 行 + polygon 几何时由父级注入，否则不渲染按钮。 */
  onRefine?: () => void;
  onDelete?: () => void;
  onChangeClass?: () => void;
  /** v0.10.5 M4-β · I15 切换 lock/hidden；仅人工框传入。 */
  onToggleFlag?: (flag: "is_locked" | "is_hidden") => void;
  orphan?: boolean;
  rasterMaskStatus?: RasterMaskRecordStatus;
  onRetryRasterMask?: () => void;
}

export function BoxListItem({
  b,
  isAi,
  selected,
  dimmed = false,
  imageWidth,
  imageHeight,
  onSelect,
  onAccept,
  onReject,
  onRefine,
  onDelete,
  onChangeClass,
  onToggleFlag,
  orphan = false,
  rasterMaskStatus,
  onRetryRasterMask,
}: BoxListItemProps) {
  const actionIdentity = `${isAi ? "prediction" : "annotation"}:${b.id}`;
  const [actionsPinnedId, setActionsPinnedId] = useState<string | null>(null);
  const actionsPinned = actionsPinnedId === actionIdentity;
  const color = classColor(b.cls);
  const baseToolMeta = annotationToolMeta(b, imageWidth, imageHeight);
  const toolMeta =
    b.geometry?.type !== "raster_mask" || !rasterMaskStatus
      ? baseToolMeta
      : rasterMaskStatus.state === "loading"
        ? { label: baseToolMeta.label, detail: "内容加载中…" }
        : rasterMaskStatus.state === "ready"
          ? {
              label: baseToolMeta.label,
              detail: `${rasterMaskStatus.preview ? "受限预览 · 精确像素命中 · " : ""}${rasterMaskStatus.area} px · ${rasterMaskStatus.componentCount} 组件 · ${rasterMaskStatus.holeCount} 孔洞 · AABB ${pct(rasterMaskStatus.bounds.w)}×${pct(rasterMaskStatus.bounds.h)}`,
            }
          : {
              label: baseToolMeta.label,
              detail: `${rasterMaskStatus.backendReason ?? rasterMaskStatus.reason} · ${rasterMaskStatus.message}`,
            };
  const predictionSource = isAi ? getPredictionSource(b) : null;
  // v0.14.9 · OCR / doc_layout 候选: 文本摘要 + 版面 type badge (仅 AI 行展示).
  const ocrText = isAi ? ocrTextSummary(b) : null;
  const layoutBadge = isAi ? docLayoutBadge(b) : null;
  const attrSummary = isAi ? attributesSummary(b) : [];
  return (
    <div
      data-testid={`box-list-item-${b.id}`}
      onClick={(e) => onSelect({ shiftKey: e.shiftKey })}
      className={cn(
        "grid grid-cols-[minmax(0,1fr)_auto] gap-2 items-center mb-2 p-2 px-2.5 border border-border rounded-lg bg-transparent cursor-pointer select-none",
        selected && "!border-brand bg-brand/10",
        dimmed && "opacity-55",
      )}
    >
      <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-1 gap-x-2 items-center min-w-0">
        <svg className="row-span-2 w-2.5 h-2.5 rounded-full" viewBox="0 0 10 10" aria-hidden="true">
          <circle cx="5" cy="5" r="5" fill={color} />
        </svg>
        <div className="flex items-center gap-[7px] min-w-0">
          <b className="overflow-hidden text-sm text-ellipsis whitespace-nowrap">
            {displayClassName(b.cls)}
          </b>
          {isAi ? (
            <span
              className={cn(
                "inline-flex items-center gap-1 px-1.5 py-px rounded-full text-2xs font-medium whitespace-nowrap",
                predictionSource === "external_import"
                  ? "bg-status-caution-soft text-status-caution"
                  : "bg-status-info-soft text-status-info",
              )}
            >
              <Icon name={predictionSource === "external_import" ? "upload" : "sparkle"} size={8} />
              {predictionSourceLabel(predictionSource)} · {(b.conf * 100).toFixed(0)}%
            </span>
          ) : (
            <span
              className={cn(
                "inline-flex items-center gap-1 px-1.5 py-px rounded-full text-2xs font-medium whitespace-nowrap",
                b.source === "prediction_based"
                  ? "bg-muted text-muted-foreground"
                  : "bg-brand/10 text-brand",
              )}
            >
              {b.source === "prediction_based" ? "AI 采纳" : "手动"}
            </span>
          )}
          {layoutBadge && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-px rounded-full text-2xs font-medium whitespace-nowrap border border-border bg-background text-muted-foreground"
              title="版面类别"
            >
              {layoutBadge}
            </span>
          )}
          {dimmed && (
            <span
              className="px-1.5 py-px border border-border rounded-md bg-muted text-muted-foreground text-2xs"
              title="已被同类用户框（IoU > 0.7）覆盖"
            >
              已被覆盖
            </span>
          )}
          {orphan && !isAi && (
            <span
              className="inline-flex items-center gap-1 px-1.5 py-px border border-amber-500 rounded-md bg-status-caution-soft text-status-caution text-2xs whitespace-nowrap"
              title="当前项目类别配置中已不存在该类别"
            >
              <Icon name="warning" size={9} />
              已删除
            </span>
          )}
        </div>
        <div className="flex gap-1.5 items-center min-w-0 text-muted-foreground text-xs">
          <span className="shrink-0 px-1.5 py-px border border-border rounded bg-muted text-muted-foreground font-[inherit]">
            {toolMeta.label}
          </span>
          <span className="overflow-hidden text-ellipsis whitespace-nowrap">
            {rasterMaskStatus?.state === "error" || rasterMaskStatus?.state === "deferred" ? (
              <CopyableError message={toolMeta.detail} truncate />
            ) : (
              toolMeta.detail
            )}
          </span>
          {(rasterMaskStatus?.state === "error" || rasterMaskStatus?.state === "deferred") &&
            rasterMaskStatus.retryable &&
            onRetryRasterMask && (
              <button
                type="button"
                className="shrink-0 rounded border border-border px-1.5 py-px text-2xs hover:bg-muted"
                onClick={(event) => {
                  event.stopPropagation();
                  onRetryRasterMask();
                }}
                aria-label={`重试 Mask ${b.id}`}
              >
                重试
              </button>
            )}
        </div>
        {ocrText && (
          <div
            className="col-start-2 flex items-center gap-1 min-w-0 mt-0.5 text-muted-foreground text-xs"
            title={ocrText}
          >
            <Icon name="type" size={11} />
            <span className="overflow-hidden text-ellipsis whitespace-nowrap">{ocrText}</span>
          </div>
        )}
        {attrSummary.length > 0 && (
          <div className="col-start-2 flex flex-wrap gap-1 min-w-0 mt-0.5">
            {attrSummary.map((value, i) => (
              <span
                key={i}
                className="inline-flex items-center px-1.5 py-px border border-border rounded bg-muted text-muted-foreground text-2xs whitespace-nowrap"
              >
                {value}
              </span>
            ))}
          </div>
        )}
      </div>
      {/* 操作区统一：默认只留一个常驻 ⋮ 触发按钮在最右；hover 时其余操作向左浮出成工具条（AI 行 / 人工行一致） */}
      <div className="flex gap-1.5 items-center">
        <div className="relative flex items-center justify-end group/act">
          <div
            className={cn(
              "absolute right-full top-1/2 z-base flex -translate-y-1/2 items-center gap-1 rounded-lg border border-border bg-card py-0.5 pl-1.5 pr-1 shadow-md",
              "pointer-events-none translate-x-1.5 opacity-0 transition-all duration-200 ease-out",
              "group-hover/act:pointer-events-auto group-hover/act:translate-x-0 group-hover/act:opacity-100",
              actionsPinned && "!pointer-events-auto !translate-x-0 !opacity-100",
            )}
          >
            {isAi ? (
              <>
                {onAccept && (
                  <Button
                    variant="primary"
                    size="sm"
                    title="采纳预测"
                    aria-label="采纳预测"
                    onClick={(e) => {
                      e.stopPropagation();
                      onAccept();
                    }}
                    className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
                  >
                    <Icon name="check" size={12} />
                  </Button>
                )}
                {onReject && (
                  <Button
                    variant="danger"
                    size="sm"
                    title="忽略预测"
                    aria-label="忽略预测"
                    onClick={(e) => {
                      e.stopPropagation();
                      onReject();
                    }}
                    className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
                  >
                    <Icon name="x" size={12} />
                  </Button>
                )}
                {onRefine && (
                  <Button
                    size="sm"
                    title="精修 (Mask 笔刷)"
                    aria-label="精修"
                    onClick={(e) => {
                      e.stopPropagation();
                      onRefine();
                    }}
                    className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
                    data-testid={`ai-refine-${b.id}`}
                  >
                    <Icon name="edit" size={12} />
                  </Button>
                )}
              </>
            ) : (
              <>
                {onToggleFlag && (
                  <>
                    <Button
                      size="sm"
                      title={b.is_hidden ? "显示 (H)" : "隐藏 (H)"}
                      aria-label={b.is_hidden ? "显示" : "隐藏"}
                      aria-pressed={!!b.is_hidden}
                      onClick={(e) => {
                        e.stopPropagation();
                        onToggleFlag("is_hidden");
                      }}
                      className={cn(
                        "!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3",
                        !b.is_hidden && "!opacity-55",
                      )}
                    >
                      <Icon name={b.is_hidden ? "eyeOff" : "eye"} size={12} />
                    </Button>
                    <Button
                      size="sm"
                      title={b.is_locked ? "解锁 (L)" : "锁定 (L)"}
                      aria-label={b.is_locked ? "解锁" : "锁定"}
                      aria-pressed={!!b.is_locked}
                      onClick={(e) => {
                        e.stopPropagation();
                        onToggleFlag("is_locked");
                      }}
                      className={cn(
                        "!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3",
                        !b.is_locked && "!opacity-55",
                      )}
                    >
                      <Icon name={b.is_locked ? "lock" : "unlock"} size={12} />
                    </Button>
                  </>
                )}
                {onChangeClass && (
                  <Button
                    size="sm"
                    title="修改类别"
                    aria-label="修改类别"
                    onClick={(e) => {
                      e.stopPropagation();
                      onChangeClass();
                    }}
                    className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
                  >
                    <Icon name="tag" size={12} />
                  </Button>
                )}
                {onRefine && (
                  <Button
                    size="sm"
                    title={b.geometry?.type === "raster_mask" ? "编辑 Mask" : "Mask 笔刷精修"}
                    aria-label={b.geometry?.type === "raster_mask" ? "编辑 Mask" : "精修"}
                    onClick={(e) => {
                      e.stopPropagation();
                      onRefine();
                    }}
                    className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
                    data-testid={`user-refine-${b.id}`}
                  >
                    <Icon name="edit" size={12} />
                  </Button>
                )}
                {onDelete && (
                  <Button
                    variant="danger"
                    size="sm"
                    title="删除标注"
                    aria-label="删除标注"
                    onClick={(e) => {
                      e.stopPropagation();
                      onDelete();
                    }}
                    className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3"
                  >
                    <Icon name="trash" size={12} />
                  </Button>
                )}
              </>
            )}
          </div>
          <Button
            size="sm"
            title="更多操作"
            aria-label="更多操作"
            aria-expanded={actionsPinned}
            onClick={(e) => {
              e.stopPropagation();
              setActionsPinnedId((previous) =>
                previous === actionIdentity ? null : actionIdentity,
              );
            }}
            className="!w-[24px] !h-[24px] !justify-center !p-0 !rounded-md [&_svg]:!size-3 text-muted-foreground"
          >
            <Icon name="more" size={13} />
          </Button>
        </div>
      </div>
    </div>
  );
}
