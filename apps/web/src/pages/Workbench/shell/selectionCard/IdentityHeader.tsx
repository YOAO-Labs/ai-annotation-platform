import type { ReactNode } from "react";
import { Icon } from "@/components/ui/Icon";
import type { AnnotationResponse } from "@/types";
import { classColor, displayClassName } from "../../stage/colors";

export type SourceKind = "manual" | "ai" | "accepted" | "import";

export type ConfidenceTone = "high" | "mid" | "low";

export function confidenceTone(conf: number): ConfidenceTone {
  if (conf >= 0.8) return "high";
  if (conf >= 0.5) return "mid";
  return "low";
}

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
  confidence?: number | null;
  trailing?: ReactNode;
  dotColor?: string;
}

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
