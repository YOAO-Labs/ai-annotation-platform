import type { AnnotationFeedback, FeedbackSeverity, FeedbackStatus } from "@/api/feedbacks";

export const ISSUE_PIN_RADIUS_PX = 8;
export const ISSUE_PIN_STROKE_PX = 2;
export const ISSUE_PIN_SELECTED_STROKE_PX = 3;
export const ISSUE_PIN_HIGHLIGHT_RING_PX = 3;
export const ISSUE_PIN_SYMBOL_PX = 10;

const SEVERITY_COLOR_VAR: Record<FeedbackSeverity, string> = {
  info: "--sc-status-info-alt",
  warn: "--sc-status-caution",
  blocker: "--sc-status-danger",
};

const SEVERITY_LABEL: Record<FeedbackSeverity, string> = {
  info: "提示",
  warn: "警告",
  blocker: "阻断",
};

const STATUS_LABEL: Record<FeedbackStatus, string> = {
  open: "未解决",
  resolved: "已解决",
  wont_fix: "搁置",
};

export function issuePinColorVar(
  status: FeedbackStatus,
  severity: FeedbackSeverity | null | undefined,
): string {
  if (status === "resolved") return "--sc-status-positive";
  if (status === "wont_fix") return "--sc-muted-foreground";
  return severity ? SEVERITY_COLOR_VAR[severity] : SEVERITY_COLOR_VAR.warn;
}

export function issuePinSymbol(
  status: FeedbackStatus,
  severity: FeedbackSeverity | null | undefined,
): string {
  if (status === "resolved") return "✓";
  if (status === "wont_fix") return "–";
  if (severity === "info") return "i";
  if (severity === "blocker") return "×";
  return "!";
}

export function issuePinAriaLabel(issue: Pick<AnnotationFeedback, "status" | "severity">): string {
  const severity = issue.severity ? SEVERITY_LABEL[issue.severity] : "警告";
  return `Issue：${STATUS_LABEL[issue.status]} · ${severity}`;
}

export function shortIssueObjectId(id: string): string {
  return id.length > 10 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id;
}

export function issueObjectLabel(id: string, className?: string | null): string {
  return `${className?.trim() || "对象"} · ${shortIssueObjectId(id)}`;
}
