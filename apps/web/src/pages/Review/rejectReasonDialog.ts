// plan 1789527942 · T2 · Review 退回两步决策流（原 RejectReasonModal 的 Projects/Review 调用点迁移）
// 第一步 choiceDialog 选 reason_type（labels 沿用 rejectReasonTypes 单点），
// 第二步可选 inputDialog 补充说明；任一步取消 → null（放弃整次退回）。
// 注意：每一步 await 之后调用方必须重查自身作用域守卫（如 ReviewPage 的 queueScopeKeyRef）再提交。
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

/**
 * 退回原因两步弹窗。count 沿用原 modal 的「退回原因（N 个任务）」标题；
 * skipReasonHint 为单任务且被标注员跳过时的提示（原紫色提示块，纯文本展示在第一步描述里）。
 */
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
  // 原 modal 的行为:被跳过任务的补充说明预填「标注员跳过：…」,标注员可直接改写;
  // 不预填的话,直接确认会得到 reason: undefined,指派方收不到跳过原因。
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
