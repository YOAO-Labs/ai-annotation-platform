import { choiceDialog, inputDialog } from "@/components/ui/decisionDialog";

import {
  REJECT_REASON_TYPE_LABELS,
  REJECT_REASON_TYPE_ORDER,
  type RejectPayload,
  type RejectReasonType,
} from "./rejectReasonTypes";

export const REJECT_PRESET_COMMENTS: string[] = [
  "边缘漏标",
  "重叠遮挡未拆分",
  "目标类别错误",
  "位置与边界不准",
  "背景噪点误检",
  "截断漏标",
];

export async function promptRejectReason(options: {
  count: number;
  skipReasonHint?: string | null;
}): Promise<RejectPayload | null> {
  const reasonType = await choiceDialog({
    title: `退回原因（${options.count} 个任务）`,
    description: options.skipReasonHint
      ? `此任务被标注员跳过：${options.skipReasonHint}。退回后会重新派给其他标注员；如果该任务确实无可标注目标，建议改为「通过」。`
      : undefined,
    options: REJECT_REASON_TYPE_ORDER.map((t) => ({
      key: t,
      label: REJECT_REASON_TYPE_LABELS[t],
    })),
  });
  if (!reasonType) return null;
  const comment = await inputDialog({
    title: "补充说明",
    label: "补充说明（可选）",
    placeholder: "（可选）补充说明，例如具体目标 / 帧号 …",
    initialValue: options.skipReasonHint ? `标注员跳过：${options.skipReasonHint}` : undefined,
    confirmLabel: "确认退回",
    tone: "danger",
    presets: REJECT_PRESET_COMMENTS,
  });
  if (comment === null) return null;
  return {
    reason_type: reasonType as RejectReasonType,
    reason: comment.length > 0 ? comment : undefined,
  };
}
