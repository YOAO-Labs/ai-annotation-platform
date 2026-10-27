import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { AttributeSchema } from "@/api/projects";
import { predictionSourceLabel, type AiBox } from "../../state/transforms";
import { AttributeForm } from "../AttributeForm";
import { IdentityHeader } from "./IdentityHeader";
import { ConfidenceBar } from "./ConfidenceBar";
import { MetricGrid } from "./MetricGrid";
import { ActionBar } from "./ActionBar";
import { geometryMetrics } from "./geometryMetrics";
import { CopyableError } from "@/components/ui/CopyableError";
import type { RasterMaskRecordStatus } from "../../stage/shared/useRasterMaskRecords";

const BODY_CLASS = "flex min-h-0 flex-col gap-2.5 overflow-x-hidden overflow-y-auto px-3 pt-2.5";
const SOURCE_ROW_CLASS =
  "flex min-w-0 items-center gap-1 text-xs text-muted-foreground [&_span]:whitespace-nowrap";
const OCR_CLASS = "flex min-w-0 items-center gap-1 text-xs text-muted-foreground";
const OCR_TEXT_CLASS = "truncate";
const ATTR_BLOCK_CLASS = "border-t border-border pt-2";

export interface AIPredictionCardContentProps {
  box: AiBox;
  imageWidth: number | null;
  imageHeight: number | null;
  /** 项目属性 schema:候选携带 attributes 时只读预览(采纳前可见车型/颜色等)。 */
  attributeSchema?: AttributeSchema;
  /** 任务级锁定(review/completed)→ 采纳 / 精修 / 忽略禁用。 */
  readOnly: boolean;
  onAccept: (b: AiBox) => void;
  onReject: (b: AiBox) => void;
  /** 仅 polygon 几何时由调用方注入(开 Mask 笔刷);非 polygon 不渲染精修。 */
  onRefine: (b: AiBox) => void;
  rasterMaskStatus?: RasterMaskRecordStatus;
  onRetryRasterMask?: (id: string) => void;
}

/** OCR / doc_layout 候选携带的识别文本摘要(单行截断),无文本返回 null。 */
function ocrTextSummary(box: AiBox): string | null {
  const text = box.attributes?.text;
  if (typeof text !== "string" || text.trim() === "") return null;
  const trimmed = text.trim();
  return trimmed.length > 48 ? `${trimmed.slice(0, 48)}…` : trimmed;
}

/** v0.18.0 · 候选除 text(OCR 已单列)外是否还带可预览的二阶段属性(车型 / 颜色等)。 */
function hasPreviewableAttrs(box: AiBox): boolean {
  const attrs = box.attributes;
  if (!attrs) return false;
  return Object.entries(attrs).some(
    ([k, v]) => k !== "text" && v != null && v !== "" && typeof v !== "object",
  );
}

/**
 * v0.16.14 · 选中 AI 预测框的浮动卡内容(图片端专属)。
 * 补齐此前掉进 placeholder 而丢失的信息:大置信度条 + 来源 / 候选序号 + OCR 文本,
 * 动作组直连模型既有 handleAcceptPrediction / handleRefinePrediction / handleRejectPrediction,
 * 不新增业务逻辑。精修按钮仅 polygon 几何渲染(与右栏 AIInspectorPanel 同条件)。
 */
export function AIPredictionCardContent({
  box,
  imageWidth,
  imageHeight,
  attributeSchema,
  readOnly,
  onAccept,
  onReject,
  onRefine,
  rasterMaskStatus,
  onRetryRasterMask,
}: AIPredictionCardContentProps) {
  const metrics = box.geometry ? geometryMetrics(box.geometry, imageWidth, imageHeight) : [];
  const isPolygon = box.geometry?.type === "polygon";
  const ocrText = ocrTextSummary(box);
  // 采纳前预览二阶段属性:项目已配 schema 且候选带 attributes 时, 只读展示(经 options 解析中文)。
  const schemaFields = attributeSchema?.fields ?? [];
  const showAttrPreview = schemaFields.length > 0 && hasPreviewableAttrs(box);

  return (
    <div className={BODY_CLASS}>
      <IdentityHeader className={box.cls} source="ai" />

      <ConfidenceBar value={box.conf} />

      <div className={SOURCE_ROW_CLASS}>
        <Icon name="bot" size={12} />
        <span>{predictionSourceLabel(box.predictionSource)}</span>
        <span aria-hidden="true">·</span>
        <span>第 {box.shapeIndex + 1} 个候选</span>
        {box.tool_unit_id && (
          <>
            <span aria-hidden="true">·</span>
            <span>{box.tool_unit_id}</span>
          </>
        )}
      </div>

      {ocrText && (
        <div className={OCR_CLASS} title={ocrText}>
          <Icon name="type" size={11} />
          <span className={OCR_TEXT_CLASS}>{ocrText}</span>
        </div>
      )}

      <MetricGrid metrics={metrics} />

      {box.geometry?.type === "raster_mask" && (
        <div
          role="status"
          className="rounded-md border border-border bg-muted px-2.5 py-2 text-xs leading-5 text-muted-foreground"
        >
          {!rasterMaskStatus ? (
            "Mask 内容尚未加载"
          ) : rasterMaskStatus.state === "loading" ? (
            "Mask 内容加载中…"
          ) : rasterMaskStatus.state === "ready" ? (
            `${rasterMaskStatus.preview ? "受限预览 · 精确像素命中" : "已按真实像素渲染"} · ${rasterMaskStatus.area} px · ${rasterMaskStatus.componentCount} 个组件 · ${rasterMaskStatus.holeCount} 个孔洞`
          ) : (
            <>
              <CopyableError message={rasterMaskStatus.message} />
              {rasterMaskStatus.retryable && onRetryRasterMask && (
                <Button size="xs" variant="ghost" onClick={() => onRetryRasterMask(box.id)}>
                  重试加载
                </Button>
              )}
            </>
          )}
        </div>
      )}

      {showAttrPreview && (
        <div className={ATTR_BLOCK_CLASS} data-floating-panel-no-drag>
          <AttributeForm
            schema={attributeSchema}
            className={box.cls}
            attributes={box.attributes ?? {}}
            onChange={() => {}}
            readOnly
            context="image"
            hideHeading
          />
        </div>
      )}

      <ActionBar label="预测操作">
        <Button
          variant="primary"
          size="xs"
          title="采纳预测"
          disabled={readOnly}
          onClick={() => onAccept(box)}
        >
          <Icon name="check" size={12} />
          采纳
        </Button>
        {isPolygon && (
          <Button
            variant="ghost"
            size="xs"
            title="精修(Mask 笔刷)"
            disabled={readOnly}
            onClick={() => onRefine(box)}
          >
            <Icon name="edit" size={12} />
            精修
          </Button>
        )}
        <Button
          variant="danger"
          size="xs"
          title="忽略预测"
          disabled={readOnly}
          onClick={() => onReject(box)}
        >
          <Icon name="x" size={12} />
          忽略
        </Button>
      </ActionBar>
    </div>
  );
}
