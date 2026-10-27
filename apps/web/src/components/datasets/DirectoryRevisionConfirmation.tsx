import { useState } from "react";
import type { DatasetSourceChangeOut } from "@/api/generated/types.gen";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useVerifiedImage } from "@/hooks/useVerifiedImage";

export function DirectoryRevisionConfirmation({
  entry,
  scanId,
  busy,
  error,
  onClose,
  onConfirm,
}: {
  entry: DatasetSourceChangeOut;
  scanId: string;
  busy: boolean;
  error: string;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const [reviewed, setReviewed] = useState(false);
  const revalidate = entry.can_revalidate;
  const image = useVerifiedImage(
    `/dataset-scans/${scanId}/entries/${entry.path_digest}/content?sha256=${entry.candidate_sha256}`,
  );
  return (
    <Modal
      open
      title={revalidate ? "重新核验并保留样本" : "接受内容为新修订"}
      onClose={() => {
        if (!busy) onClose();
      }}
      width={640}
    >
      <p className="break-all font-medium">{entry.relative_path}</p>
      <p className="mt-2">
        {revalidate
          ? "候选与原图摘要相同。确认后按新的文件观测严格读取，继续使用原样本、任务和标注，并保留核验历史。"
          : "为这一项创建新样本，并在已关联的图片项目中创建空白任务。旧样本、任务和标注保留；旧目录文件已被覆盖时，旧原图可能无法读取。"}
      </p>
      <dl className="my-3 space-y-2 break-all text-xs">
        <div>
          <dt className="text-muted-foreground">旧样本</dt>
          <dd>{entry.before_item_id}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">旧修订</dt>
          <dd>{entry.before_revision_id}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">旧 SHA256</dt>
          <dd>{entry.before_sha256}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">候选 SHA256</dt>
          <dd>{entry.candidate_sha256}</dd>
        </div>
      </dl>
      {image.url && (
        <img
          src={image.url}
          alt="待接纳修订的核验原图"
          className="max-h-64 w-full rounded-md object-contain"
        />
      )}
      {image.error && (
        <p role="alert" className="text-status-danger">
          {image.error}
        </p>
      )}
      {!image.url && (
        <Button size="sm" disabled={busy} onClick={image.retry}>
          重新读取候选
        </Button>
      )}
      <label className="mt-3 flex items-start gap-2">
        <input
          type="checkbox"
          checked={reviewed}
          disabled={busy}
          onChange={(e) => setReviewed(e.target.checked)}
        />
        {revalidate
          ? "已核对同一内容，按新观测读取并保留原样本、任务和标注"
          : "已核对这一项，创建新样本与空白任务，保留旧标注"}
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
        <Button variant="primary" disabled={busy || !reviewed || !image.url} onClick={onConfirm}>
          {busy ? "正在接纳…" : revalidate ? "确认重新核验此项" : "确认接纳此项新修订"}
        </Button>
      </div>
    </Modal>
  );
}
