import type { ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import type { AnnotationResponse } from "@/types";
import { classColor, displayClassName } from "../../stage/colors";

/** 标注来源:手动 / AI 预测(未采纳)/ AI 采纳(已落库)/ 外部导入。 */
export type SourceKind = "manual" | "ai" | "accepted" | "import";

/** 置信度三档配色键,与 Tailwind 语义色对齐。 */
export type ConfidenceTone = "high" | "mid" | "low";

/** ≥0.8 高 / 0.5–0.8 中 / <0.5 低。阈值取闭区间下界。 */
export function confidenceTone(conf: number): ConfidenceTone {
  if (conf >= 0.8) return "high";
  if (conf >= 0.5) return "mid";
  return "low";
}

/** 从已落库标注派生来源:有 parent_prediction_id = AI 采纳;source 含 import = 导入;否则手动。 */
export function annotationSourceKind(ann: AnnotationResponse): SourceKind {
  if (ann.parent_prediction_id) return "accepted";
  if (ann.source?.includes("import")) return "import";
  return "manual";
}

const HEADER_CLASS = "flex min-w-0 items-center gap-1.5";
const DOT_CLASS = "flex-none size-2.5 rounded-full";
const NAME_CLASS = "truncate text-sm font-semibold text-foreground";
const BADGE_CLASS =
  "inline-flex flex-none items-center gap-1 rounded-full px-1.5 py-px text-2xs font-medium whitespace-nowrap";
const CONF_PILL_CLASS = "flex-none rounded-full px-1.5 py-px text-xs font-semibold tabular-nums";

const SOURCE_META: Record<
  SourceKind,
  { label: string; icon: "tag" | "sparkle" | "check" | "upload"; badgeClass: string }
> = {
  manual: { label: "手动", icon: "tag", badgeClass: "bg-muted text-muted-foreground" },
  ai: { label: "AI 预测", icon: "sparkle", badgeClass: "bg-status-info-soft text-status-info" },
  accepted: {
    label: "AI 采纳",
    icon: "check",
    badgeClass: "bg-status-positive-soft text-status-positive",
  },
  import: {
    label: "导入",
    icon: "upload",
    badgeClass: "bg-status-caution-soft text-status-caution",
  },
};

const TONE_CLASS: Record<ConfidenceTone, string> = {
  high: "bg-status-positive-soft text-status-positive",
  mid: "bg-status-caution-soft text-status-caution",
  low: "bg-status-danger-soft text-status-danger",
};

export interface IdentityHeaderProps {
  className: string;
  source: SourceKind;
  /** 0–1。传入时在右侧渲染置信度 pill(按阈值着色)。手动框不传。 */
  confidence?: number | null;
  /** 右侧附加位(如视频单帧的「F12 · 00:24」帧定位)。 */
  trailing?: ReactNode;
  /** 色块覆盖色(如视频轨迹的 getTrackColor,含逐轨道覆盖);缺省回落到 classColor。 */
  dotColor?: string;
}

/**
 * 选中信息卡通用身份头:类别色块 + 类名 + 来源徽章 +(可选)置信度 pill / 帧定位。
 * 四种选中态共用,色块走数据域类别色(与画布/列表同源),徽章/pill 走 Tailwind 语义类。
 */
export function IdentityHeader({
  className,
  source,
  confidence,
  trailing,
  dotColor,
}: IdentityHeaderProps) {
  const meta = SOURCE_META[source];
  const showConf = typeof confidence === "number";
  return (
    <div className={HEADER_CLASS}>
      <span
        className={DOT_CLASS}
        // eslint-disable-next-line no-restricted-syntax -- 色块为数据域颜色(同画布/列表),非主题 token;轨迹经 dotColor 传入逐轨道色。
        style={{ background: dotColor ?? classColor(className) }}
        aria-hidden="true"
      />
      <span className={NAME_CLASS} title={displayClassName(className)}>
        {displayClassName(className)}
      </span>
      <span className={`${BADGE_CLASS} ${meta.badgeClass}`}>
        <Icon name={meta.icon} size={9} />
        {meta.label}
      </span>
      <span className="flex-1" />
      {trailing}
      {showConf && (
        <span className={`${CONF_PILL_CLASS} ${TONE_CLASS[confidenceTone(confidence!)]}`}>
          {(confidence! * 100).toFixed(0)}%
        </span>
      )}
    </div>
  );
}
