import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useState } from "react";
import { datasetsApi, type DatasetResponse } from "@/api/datasets";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { DirectoryReferencePanel } from "./DirectoryReferencePanel";
import { verifiedContentError } from "@/hooks/useVerifiedImage";

type Props = { onClose: () => void; onCreated: (dataset: DatasetResponse) => void };

export function CreateReferenceDatasetModal(props: Props) {
  const revision = useSessionRevision();
  return <CreateReferenceDatasetModalOwner key={revision} {...props} />;
}

function CreateReferenceDatasetModalOwner({ onClose, onCreated }: Props) {
  const capture = useSessionGuard();
  const [name, setName] = useState("");
  const [dataset, setDataset] = useState<DatasetResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal open onClose={onClose} title="接入服务器目录">
      {dataset ? (
        <>
          <p className="mb-3 text-sm">
            数据集 {dataset.name} 已创建。配置来源并登记图片后，可在数据集详情关联项目。
          </p>
          <DirectoryReferencePanel datasetId={dataset.id} />
          <Button onClick={onClose}>返回数据集列表</Button>
        </>
      ) : (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            const current = capture();
            setBusy(true);
            setError("");
            void datasetsApi
              .create({ name: name.trim(), data_type: "image" })
              .then((created) => {
                if (current()) {
                  setDataset(created);
                  onCreated(created);
                }
              })
              .catch((e) => {
                if (current()) setError(verifiedContentError(e));
              })
              .finally(() => {
                if (current()) setBusy(false);
              });
          }}
        >
          <p className="mb-3 text-sm text-muted-foreground">
            从管理员已挂载并登记的位置接入图片，保留原图位置。电脑上的文件夹请使用“新建数据集”的上传流程。
          </p>
          <label className="grid gap-1 text-xs">
            数据集名称
            <input
              className="rounded-md border border-input bg-background px-2 py-2 text-sm text-foreground"
              value={name}
              onChange={(e) => setName(e.target.value)}
              minLength={2}
              maxLength={60}
              required
            />
          </label>
          {error && (
            <p role="alert" className="mt-2 text-xs text-status-danger">
              {error}
            </p>
          )}
          <div className="mt-3 flex justify-end gap-2">
            <Button type="button" onClick={onClose}>
              取消
            </Button>
            <Button type="submit" variant="primary" disabled={busy || name.trim().length < 2}>
              创建并配置目录
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
