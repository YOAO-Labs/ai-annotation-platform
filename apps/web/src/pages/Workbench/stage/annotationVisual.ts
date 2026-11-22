import type { LabelContentByType, LabelFieldToken } from "@/api/auth";

export type { LabelContentByType, LabelFieldToken };

export type LabelVisibility = "always" | "selected" | "none";

export const VISUAL_DEFAULTS = {
  labelFontSize: 12,
  strokeWidth: 1.5,
  fillOpacity: 0.07,
  fillOpacitySelected: 0.12,
} as const;

export const SELECTED_STROKE_BONUS = 0.5;

export interface AnnotationVisualConfig {
  labelFontSize: number;
  strokeWidth: number;
  fillOpacity: number;
  fillOpacitySelected: number;
  labelVisibility: LabelVisibility;
  labelContent: LabelContentByType;
}

export const DEFAULT_ANNOTATION_VISUAL: AnnotationVisualConfig = {
  labelFontSize: VISUAL_DEFAULTS.labelFontSize,
  strokeWidth: VISUAL_DEFAULTS.strokeWidth,
  fillOpacity: VISUAL_DEFAULTS.fillOpacity,
  fillOpacitySelected: VISUAL_DEFAULTS.fillOpacitySelected,
  labelVisibility: "always",
  labelContent: { single: [], track: ["id", "state"], ai: ["source", "score"] },
};

export function resolveAnnotationVisual(common: AnnotationVisualConfig): AnnotationVisualConfig {
  return {
    labelFontSize: common.labelFontSize,
    strokeWidth: common.strokeWidth,
    fillOpacity: common.fillOpacity,
    fillOpacitySelected: common.fillOpacitySelected,
    labelVisibility: common.labelVisibility,
    labelContent: common.labelContent,
  };
}

export function strokeWidthFor(
  selected: boolean,
  cfg: Pick<AnnotationVisualConfig, "strokeWidth">,
): number {
  return selected ? cfg.strokeWidth + SELECTED_STROKE_BONUS : cfg.strokeWidth;
}

export function fillAlpha(
  selected: boolean,
  cfg: Pick<AnnotationVisualConfig, "fillOpacity" | "fillOpacitySelected">,
): number {
  return selected ? cfg.fillOpacitySelected : cfg.fillOpacity;
}

export function shouldShowLabel(selected: boolean, visibility: LabelVisibility): boolean {
  if (visibility === "none") return false;
  if (visibility === "selected") return selected;
  return true;
}

export interface LabelTextInput {
  className: string;
  instanceId?: string | number | null;
  confidence?: number | null;
  attributes?: Record<string, unknown> | null;
  sourcePrefix?: string;
}

export function buildLabelText(input: LabelTextInput, content: readonly LabelFieldToken[]): string {
  const tokens: string[] = [input.className];
  if (content.includes("id") && input.instanceId != null && input.instanceId !== "") {
    tokens.push(`#${input.instanceId}`);
  }
  if (content.includes("score") && typeof input.confidence === "number") {
    tokens.push(`${Math.round(input.confidence * 100)}%`);
  }
  if (content.includes("attrs")) {
    const attrs = formatAttributes(input.attributes);
    if (attrs) tokens.push(attrs);
  }
  const core = tokens.join(" · ");
  return content.includes("source") && input.sourcePrefix ? `${input.sourcePrefix}${core}` : core;
}

export interface TrackLabelInput {
  className: string;
  trackNumber?: number;
  stateSuffix?: string;
  attributes?: Record<string, unknown> | null;
}

export function buildTrackLabelText(
  input: TrackLabelInput,
  content: readonly LabelFieldToken[],
): string {
  const parts: string[] = [input.className];
  if (content.includes("attrs")) {
    const attrs = formatAttributes(input.attributes);
    if (attrs) parts.push(attrs);
  }
  let text = parts.join(" · ");
  if (content.includes("id") && input.trackNumber !== undefined) {
    text = `#${input.trackNumber} · ${text}`;
  }
  if (content.includes("state") && input.stateSuffix) {
    text = `${text} · ${input.stateSuffix}`;
  }
  return text;
}

function formatAttributes(attrs?: Record<string, unknown> | null): string {
  if (!attrs) return "";
  const parts: string[] = [];
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("_")) continue;
    if (v == null || v === "") continue;
    if (typeof v === "boolean") {
      if (v) parts.push(k);
      continue;
    }
    parts.push(String(v));
  }
  return parts.join(" · ");
}
