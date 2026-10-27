import { listStyles } from "@/components/ui/listStyles";
import { CopyableError } from "@/components/ui/CopyableError";
import type { ReusableSnapshot, TrainingReuseCandidate } from "@/api/trainingRecipes";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { trainingValueLabel } from "./TrainingParameterField";

export function effectiveChanges(before: ReusableSnapshot | null, after: ReusableSnapshot) {
  const old = before?.configuration.effective_values ?? {};
  const next = after.configuration.effective_values;
  return [...new Set([...Object.keys(old), ...Object.keys(next)])]
    .sort()
    .filter((key) => JSON.stringify(old[key]) !== JSON.stringify(next[key]))
    .map((key) => ({ key, old: old[key], next: next[key] }));
}

export function TrainingReuseDialog({
  candidate,
  before,
  busy,
  error,
  onCancel,
  onAccept,
  purpose = "experiment",
}: {
  candidate: TrainingReuseCandidate;
  before: ReusableSnapshot | null;
  busy: boolean;
  error: string;
  onCancel: () => void;
  onAccept: () => void;
  purpose?: "experiment" | "policy";
}) {
  const after = candidate.snapshot as ReusableSnapshot;
  const changes = effectiveChanges(before, after);
  return (
    <Modal open onClose={() => !busy && onCancel()} title="核对复用配置" width={760}>
      <div className="space-y-4 text-xs">
        <p className="break-words font-medium">{candidate.label}</p>
        <p className="break-all text-muted-foreground">
          {after.framework} {after.framework_version} · 原始基础权重 {after.base_model.id}
          <br />
          SHA256：
          <CopyValueButton value={after.base_model.sha256} label="复制基础权重哈希" />
        </p>
        <p className="text-muted-foreground">
          {purpose === "policy"
            ? "采用后固定这一修订的完整配置与初始化权重。保留当前数据范围；最后保存自动策略才生效。"
            : "使用保存的初始化权重新建实验，从第 0 轮开始。保留当前实验名称与已明确选择的数据；来源数据不会自动选中。仍需重新预检。"}
        </p>
        <div className="space-y-1 break-all rounded-md bg-muted p-3">
          <p>
            当前初始化：
            {before?.initialization ? (
              <>
                {before.initialization.model_name} ·{" "}
                <CopyValueButton
                  value={before.initialization.artifact.sha256}
                  label="复制当前初始化权重哈希"
                />
              </>
            ) : (
              "预训练基础权重"
            )}
          </p>
          <p>
            采用后初始化：
            {after.initialization ? (
              <>
                {after.initialization.model_name} ·{" "}
                <CopyValueButton
                  value={after.initialization.artifact.sha256}
                  label="复制采用后初始化权重哈希"
                />
              </>
            ) : (
              "预训练基础权重"
            )}
          </p>
          {after.initialization && (
            <p className="text-muted-foreground">
              固定来源{" "}
              <CopyValueButton
                value={after.initialization.model_version_id}
                kind="id"
                label="复制固定来源模型版本 ID"
              />
              ；不会自动使用被克隆实验的新产物。
            </p>
          )}
        </div>
        {candidate.source_dataset_id && (
          <p className="break-all text-muted-foreground">
            来源数据版本：
            <CopyValueButton
              value={candidate.source_dataset_id}
              kind="id"
              label="复制来源数据版本 ID"
            />
          </p>
        )}
        {(!candidate.compatible || error) && (
          <p role="alert" className="text-status-danger">
            <CopyableError message={error || candidate.incompatibility} />
          </p>
        )}
        <p>
          {before
            ? `完整生效参数差异：${changes.length} 项`
            : "当前尚无完整配置；以下为来源的全部生效参数。"}
        </p>
        <div className="max-h-72 overflow-auto rounded-md border border-border">
          <table className={listStyles.table}>
            <thead>
              <tr className="border-b border-border">
                <th scope="col" className="p-2">
                  参数
                </th>
                <th scope="col" className="p-2">
                  当前
                </th>
                <th scope="col" className="p-2">
                  采用后
                </th>
              </tr>
            </thead>
            <tbody>
              {changes.map((change) => (
                <tr key={change.key} className="border-b border-border last:border-0">
                  <th scope="row" className="break-all p-2 font-normal">
                    {change.key}
                  </th>
                  <td className="break-all p-2 text-muted-foreground">
                    {change.old === undefined ? "未配置" : trainingValueLabel(change.old)}
                  </td>
                  <td className="break-all p-2">{trainingValueLabel(change.next)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="flex justify-end gap-2">
          <Button size="sm" variant="ghost" disabled={busy} onClick={onCancel}>
            取消复用
          </Button>
          <Button size="sm" disabled={busy || !candidate.compatible} onClick={onAccept}>
            {busy ? "核验来源…" : "确认采用配置"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
