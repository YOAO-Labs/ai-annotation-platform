/**
 * 标注员跳过任务 modal。
 *
 * 4 项预设原因（image_corrupt / no_target / unclear / other）+ 可选 note。
 * 确认后 POST /tasks/{id}/skip，由父组件触发 invalidate + 切下一题。
 */
import { useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";

export type SkipReason = "image_corrupt" | "no_target" | "unclear" | "other";

const REASON_LABELS: Record<SkipReason, string> = {
  image_corrupt: "图像损坏 / 无法打开",
  no_target: "图中无目标可标",
  unclear: "图像不清晰 / 难以判断",
  other: "其他（请补充说明）",
};

function cn(...classes: Array<string | false | null | undefined>): string {
  return classes.filter(Boolean).join(" ");
}

interface SkipTaskModalProps {
  open: boolean;
  isSubmitting?: boolean;
  onClose: () => void;
  onConfirm: (reason: SkipReason, note?: string) => void;
}

export function SkipTaskModal({ open, isSubmitting, onClose, onConfirm }: SkipTaskModalProps) {
  const [reason, setReason] = useState<SkipReason>("image_corrupt");
  const [note, setNote] = useState("");

  const canConfirm = reason !== "other" || note.trim().length > 0;

  return (
    <Modal open={open} onClose={onClose} title="跳过任务" width={420}>
      <div className="flex flex-col gap-2.5">
        <p className="m-0 text-sm text-muted-foreground">
          被跳过的任务会自动转给审核员复核；请选择主要原因。
        </p>
        {(Object.keys(REASON_LABELS) as SkipReason[]).map((r) => (
          <label
            key={r}
            className={cn(
              "flex cursor-pointer items-center gap-2 rounded-md border border-border bg-transparent px-2.5 py-2 text-sm text-foreground",
              reason === r && "border-foreground/30 bg-muted",
            )}
            data-testid={`skip-reason-${r}`}
          >
            <input
              type="radio"
              name="skip-reason"
              value={r}
              checked={reason === r}
              onChange={() => setReason(r)}
              className="cursor-pointer accent-brand"
            />
            <span>{REASON_LABELS[r]}</span>
          </label>
        ))}
        {reason === "other" && (
          <textarea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="补充说明…"
            rows={3}
            className="w-full resize-y appearance-none rounded-md border border-border bg-card p-2 text-sm text-foreground [font:inherit]"
            data-testid="skip-reason-note"
          />
        )}
        <div className="mt-1.5 flex justify-end gap-2">
          <Button onClick={onClose} disabled={isSubmitting}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!canConfirm || isSubmitting}
            onClick={() => onConfirm(reason, reason === "other" ? note.trim() : undefined)}
            data-testid="skip-confirm"
          >
            {isSubmitting ? "提交中..." : "确认跳过"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
