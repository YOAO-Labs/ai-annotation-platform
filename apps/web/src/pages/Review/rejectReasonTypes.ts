export type RejectReasonType = "missing" | "extra" | "wrong_label" | "wrong_geometry";

export interface RejectPayload {
  reason_type: RejectReasonType;
  reason?: string;
}

export const REJECT_REASON_TYPE_LABELS: Record<RejectReasonType, string> = {
  missing: "漏标",
  extra: "多标",
  wrong_label: "类别错误",
  wrong_geometry: "位置或尺寸不准",
};

export const REJECT_REASON_TYPE_ORDER: RejectReasonType[] = [
  "missing",
  "extra",
  "wrong_label",
  "wrong_geometry",
];
