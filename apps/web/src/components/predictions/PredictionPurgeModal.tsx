import { useEffect, useMemo, useState } from "react";

import {
  predictionsApi,
  type PredictionPurgeResult,
  type PredictionPurgeSourceScope,
} from "@/api/predictions";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";

import styles from "./PredictionImportWizard.module.css";

interface Props {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onComplete?: (result: PredictionPurgeResult) => void;
}

const SOURCE_OPTIONS: Array<{
  value: PredictionPurgeSourceScope;
  label: string;
}> = [
  { value: "external_import", label: "外部导入预测" },
  { value: "ml_backend", label: "模型服务预标" },
  { value: "all", label: "全部预测" },
];

export function PredictionPurgeModal({ open, projectId, onClose, onComplete }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const [sourceScope, setSourceScope] = useState<PredictionPurgeSourceScope>("external_import");
  const [preview, setPreview] = useState<PredictionPurgeResult | null>(null);
  const [loadingPreview, setLoadingPreview] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [acknowledged, setAcknowledged] = useState(false);

  const risky = sourceScope === "ml_backend" || sourceScope === "all";
  const total = preview?.counts.total ?? 0;
  const canSubmit = total > 0 && (!risky || acknowledged) && !submitting;

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoadingPreview(true);
    setPreview(null);
    setAcknowledged(false);
    predictionsApi
      .purge(projectId, {
        source_scope: sourceScope,
        task_ids: null,
        dry_run: true,
      })
      .then((result) => {
        if (!cancelled) setPreview(result);
      })
      .catch((err) => {
        if (!cancelled) {
          pushToast({
            msg: "统计预测数量失败",
            sub: err instanceof Error ? err.message : undefined,
            kind: "error",
          });
        }
      })
      .finally(() => {
        if (!cancelled) setLoadingPreview(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, projectId, pushToast, sourceScope]);

  const sourceLabel = useMemo(
    () => SOURCE_OPTIONS.find((option) => option.value === sourceScope)?.label ?? sourceScope,
    [sourceScope],
  );

  const close = () => {
    if (submitting) return;
    setSourceScope("external_import");
    setPreview(null);
    setAcknowledged(false);
    onClose();
  };

  const confirm = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const result = await predictionsApi.purge(projectId, {
        source_scope: sourceScope,
        task_ids: null,
        dry_run: false,
      });
      pushToast({
        msg: `已清理 ${result.counts.total} 条预测`,
        kind: "success",
      });
      onComplete?.(result);
      setSourceScope("external_import");
      setPreview(null);
      setAcknowledged(false);
      onClose();
    } catch (err) {
      pushToast({
        msg: "清理预测失败",
        sub: err instanceof Error ? err.message : undefined,
        kind: "error",
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal open={open} onClose={close} title="清理预测" width={520}>
      <div className={styles.wizard}>
        <div className={styles.formRow}>
          <label htmlFor="prediction-purge-source" className={styles.formLabel}>
            来源范围
          </label>
          <select
            id="prediction-purge-source"
            className={styles.input}
            value={sourceScope}
            onChange={(e) => setSourceScope(e.target.value as PredictionPurgeSourceScope)}
            disabled={submitting}
          >
            {SOURCE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        <div className={styles.previewBox}>
          <div className={styles.previewStats}>
            <div className={styles.previewStat}>
              <span className={styles.previewStatLabel}>将清理</span>
              <span className={styles.previewStatValue}>{loadingPreview ? "..." : total}</span>
            </div>
            <div className={styles.previewStat}>
              <span className={styles.previewStatLabel}>模型服务</span>
              <span className={styles.previewStatValue}>{preview?.counts.ml_backend ?? 0}</span>
            </div>
            <div className={styles.previewStat}>
              <span className={styles.previewStatLabel}>外部导入</span>
              <span className={styles.previewStatValue}>
                {preview?.counts.external_import ?? 0}
              </span>
            </div>
            {(preview?.counts.unknown ?? 0) > 0 && (
              <div className={styles.previewStat}>
                <span className={styles.previewStatLabel}>其他来源</span>
                <span className={styles.previewStatValue}>{preview?.counts.unknown ?? 0}</span>
              </div>
            )}
          </div>
          <div className={styles.purgeNote}>
            本次只清理当前项目的 {sourceLabel}，不会删除已采纳的人工标注。
          </div>
        </div>

        {risky && (
          <label className={styles.checkboxRow}>
            <input
              type="checkbox"
              checked={acknowledged}
              onChange={(e) => setAcknowledged(e.target.checked)}
              disabled={submitting}
            />
            清理模型服务预标后需要重新运行模型才能恢复。
          </label>
        )}

        <div className={styles.actions}>
          <Button variant="ghost" onClick={close} disabled={submitting}>
            取消
          </Button>
          <Button variant="danger" onClick={confirm} disabled={!canSubmit || loadingPreview}>
            {submitting ? "清理中..." : "确认清理"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
