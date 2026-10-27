import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { datasetSourcesApi as api } from "@/api/datasetSources";
import type { DatasetSourceChangeOut, SourceMoveCandidate } from "@/api/generated/types.gen";
import { useAuthStore } from "@/stores/authStore";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { useVerifiedImage, verifiedContentError } from "@/hooks/useVerifiedImage";

export function DirectoryMoveConfirmation({
  entry,
  registrationId,
  scanId,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  entry: DatasetSourceChangeOut;
  registrationId: string;
  scanId: string;
  busy: boolean;
  error: string;
  onClose: () => void;
  onConfirm: (target: SourceMoveCandidate) => void;
}) {
  const userId = useAuthStore((state) => state.user?.id);
  const sessionId = useAuthStore((state) => state.sessionId);
  const [offset, setOffset] = useState(0);
  const [target, setTarget] = useState<SourceMoveCandidate | null>(null);
  const [reviewed, setReviewed] = useState(false);
  const candidates = useQuery({
    queryKey: [
      "directory-move-candidates",
      userId,
      sessionId,
      registrationId,
      entry.before_revision_id,
      offset,
    ],
    queryFn: ({ signal }) => api.moveCandidates(registrationId, entry.path_digest, offset, signal),
  });
  const image = useVerifiedImage(
    target
      ? `/dataset-scans/${scanId}/entries/${target.path_digest}/content?sha256=${target.candidate_sha256}`
      : null,
  );
  return (
    <Modal
      open
      title="确认移动并保留样本"
      onClose={() => {
        if (!busy) onClose();
      }}
      width={640}
    >
      <p>
        旧路径：<span className="break-all font-medium">{entry.relative_path}</span>
      </p>
      <p className="mt-2">
        本轮未见不代表文件已删除。以下新增路径具有相同内容摘要，请明确选择移动目标；确认后原样本、任务和标注按目标路径继续使用。
      </p>
      <p className="my-2 break-all text-xs text-muted-foreground">
        原样本 {entry.before_item_id} · SHA256 {entry.before_sha256}
      </p>
      {candidates.error && (
        <p role="alert" className="text-status-danger">
          {verifiedContentError(candidates.error)}
        </p>
      )}
      {candidates.isPending && <p role="status">正在查找同内容新增路径…</p>}
      {candidates.data?.items.map((candidate) => (
        <label key={candidate.path_digest} className="my-2 flex items-start gap-2 break-all">
          <input
            type="radio"
            name="move-target"
            checked={target?.path_digest === candidate.path_digest}
            disabled={busy}
            onChange={() => {
              setTarget(candidate);
              setReviewed(false);
            }}
          />
          {candidate.relative_path}
        </label>
      ))}
      {candidates.data?.total === 0 && (
        <p>没有可用的同内容新增路径。已有样本保持原身份；可重新扫描后核对。</p>
      )}
      <div className="my-2 flex items-center gap-2">
        <Button
          size="sm"
          disabled={busy || !offset}
          onClick={() => setOffset(Math.max(0, offset - 20))}
        >
          上一页候选
        </Button>
        <span>
          {offset + (candidates.data?.items.length ? 1 : 0)}–
          {offset + (candidates.data?.items.length ?? 0)} / {candidates.data?.total ?? 0}
        </span>
        <Button
          size="sm"
          disabled={busy || offset + 20 >= (candidates.data?.total ?? 0)}
          onClick={() => setOffset(offset + 20)}
        >
          下一页候选
        </Button>
      </div>
      {target && <p className="my-2 break-all">移动到：{target.relative_path}</p>}
      {image.url && (
        <img
          src={image.url}
          alt="移动目标的核验原图"
          className="max-h-56 w-full rounded-md object-contain"
        />
      )}
      {image.error && (
        <p role="alert" className="text-status-danger">
          {image.error}
        </p>
      )}
      {target && !image.url && (
        <Button size="sm" disabled={busy} onClick={image.retry}>
          重新读取目标
        </Button>
      )}
      <label className="mt-3 flex items-start gap-2">
        <input
          type="checkbox"
          checked={reviewed}
          disabled={busy || !target}
          onChange={(event) => setReviewed(event.target.checked)}
        />
        已核对旧路径与目标，保留原样本、任务和标注
      </label>
      {error && (
        <p role="alert" className="mt-2 text-status-danger">
          {error}
        </p>
      )}
      <div className="mt-4 flex justify-end gap-2">
        <Button disabled={busy} onClick={onClose}>
          取消
        </Button>
        <Button
          variant="primary"
          disabled={busy || !target || !reviewed || !image.url}
          onClick={() => {
            if (target) onConfirm(target);
          }}
        >
          {busy ? "正在重定位…" : "确认移动此样本"}
        </Button>
      </div>
    </Modal>
  );
}
