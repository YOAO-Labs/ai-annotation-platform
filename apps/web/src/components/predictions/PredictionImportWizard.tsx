import { CopyableError } from "@/components/ui/CopyableError";
// v0.10.15 · 外部预测导入向导.
//
// 三步: ①选格式 + 文件 + 兜底 model_version ② dry-run 预览 ③确认提交.
// 端点: POST /projects/{id}/predictions/import?format=&dry_run=
// 入库的 prediction 行 source='external_import', ml_backend_id=NULL.

import { useState, type DragEvent } from "react";
import { clsx } from "clsx";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import {
  predictionsApi,
  type PredictionImportFormat,
  type PredictionImportResult,
  type YoloImportVariant,
} from "@/api/predictions";

import styles from "./PredictionImportWizard.module.css";

type WizardStep = "select" | "preview" | "done";

// v0.10.54 · 导入对象: 预测 (predictions[]) 或标注 (annotations[], ADR-0028).
export type ImportTarget = "predictions" | "annotations";

interface Props {
  open: boolean;
  onClose: () => void;
  projectId: string;
  /** v0.10.54 · 打开时预设的导入对象 (菜单两个入口分别预设 预测 / 标注)。 */
  initialTarget?: ImportTarget;
  onComplete?: (result: PredictionImportResult) => void;
}

interface FileImportPreview {
  fileName: string;
  result: PredictionImportResult;
}

function mergeFileResults(items: FileImportPreview[]): PredictionImportResult {
  return {
    imported: items.reduce((sum, item) => sum + item.result.imported, 0),
    skipped: items.reduce((sum, item) => sum + item.result.skipped, 0),
    errors: items.flatMap((item) =>
      item.result.errors.map((err) => ({
        ...err,
        reason: `${item.fileName}: ${err.reason}`,
      })),
    ),
    dry_run: items[0]?.result.dry_run ?? false,
  };
}

function failedFileResult(fileName: string, err: unknown, dryRun: boolean): PredictionImportResult {
  return {
    imported: 0,
    skipped: 1,
    errors: [
      {
        task_match: { file_name: fileName },
        reason: err instanceof Error ? err.message : "导入失败",
      },
    ],
    dry_run: dryRun,
  };
}

export function PredictionImportWizard({
  open,
  onClose,
  projectId,
  initialTarget = "predictions",
  onComplete,
}: Props) {
  const pushToast = useToastStore((s) => s.push);

  const [step, setStep] = useState<WizardStep>("select");
  const [target, setTarget] = useState<ImportTarget>(initialTarget);
  const [format, setFormat] = useState<PredictionImportFormat>("aap_json");
  const [yoloVariant, setYoloVariant] = useState<YoloImportVariant>("det");
  const [files, setFiles] = useState<File[]>([]);
  const [modelVersion, setModelVersion] = useState("");
  const [overwriteExisting, setOverwriteExisting] = useState(initialTarget === "predictions");
  const [imageWidth, setImageWidth] = useState("");
  const [imageHeight, setImageHeight] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [preview, setPreview] = useState<PredictionImportResult | null>(null);
  const [fileResults, setFileResults] = useState<FileImportPreview[]>([]);

  const reset = () => {
    setStep("select");
    setTarget(initialTarget);
    setFormat("aap_json");
    setYoloVariant("det");
    setFiles([]);
    setModelVersion("");
    setOverwriteExisting(initialTarget === "predictions");
    setImageWidth("");
    setImageHeight("");
    setPreview(null);
    setFileResults([]);
  };

  const close = () => {
    if (submitting) return;
    reset();
    onClose();
  };

  const handleFiles = (nextFiles: FileList | File[] | null) => {
    const selected = Array.from(nextFiles ?? []);
    const normalized = format === "yolo" ? selected.slice(0, 1) : selected;
    setFiles(normalized);
    if (format === "yolo") {
      if (normalized.some((f) => !f.name.toLowerCase().endsWith(".zip"))) {
        pushToast({ msg: "请选择 YOLO zip 文件", kind: "warning" });
      }
      return;
    }
    if (normalized.some((f) => !f.name.toLowerCase().endsWith(".json"))) {
      pushToast({ msg: "请选择 JSON 文件", kind: "warning" });
    }
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    handleFiles(e.dataTransfer.files ?? null);
  };

  const importOptions = () => {
    const base = {
      modelVersion: modelVersion || undefined,
      overwriteExisting,
    };
    // COCO 默认尺寸仅对「预测 + COCO」生效; 标注导入只走 aap_json。
    if (target !== "predictions") return base;
    if (format === "yolo") return { ...base, yoloVariant };
    if (format !== "coco") return base;

    const widthText = imageWidth.trim();
    const heightText = imageHeight.trim();
    if (!widthText && !heightText) return base;
    if (!widthText || !heightText) {
      pushToast({ msg: "请同时填写 COCO 默认宽度和高度", kind: "warning" });
      return null;
    }

    const width = Number(widthText);
    const height = Number(heightText);
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) {
      pushToast({ msg: "COCO 默认宽高必须是正整数", kind: "warning" });
      return null;
    }
    return { ...base, imageWidth: width, imageHeight: height };
  };

  const runImportBatch = async (dryRun: boolean) => {
    const options = importOptions();
    if (!options) return null;

    if (target === "predictions") {
      try {
        const result = await predictionsApi.import(
          projectId,
          format,
          files.length === 1 ? files[0] : files,
          options,
          dryRun,
        );
        setFileResults([]);
        setPreview(result);
        return result;
      } catch (err) {
        const fallback = failedFileResult(files.map((file) => file.name).join(", "), err, dryRun);
        setFileResults([]);
        setPreview(fallback);
        return fallback;
      }
    }

    const results: FileImportPreview[] = [];
    for (const [index, selectedFile] of files.entries()) {
      const firstFile = index === 0;
      try {
        const result = await predictionsApi.importAnnotations(
          projectId,
          selectedFile,
          { overwrite: firstFile && overwriteExisting },
          dryRun,
        );
        results.push({ fileName: selectedFile.name, result });
      } catch (err) {
        results.push({
          fileName: selectedFile.name,
          result: failedFileResult(selectedFile.name, err, dryRun),
        });
      }
    }

    const merged = mergeFileResults(results);
    setFileResults(results);
    setPreview(merged);
    return merged;
  };

  const doDryRun = async () => {
    if (files.length === 0) {
      pushToast({ msg: "请先选择文件", kind: "warning" });
      return;
    }
    setSubmitting(true);
    try {
      const res = await runImportBatch(true);
      if (res) setStep("preview");
    } catch (err) {
      pushToast({
        msg: err instanceof Error ? err.message : "解析失败",
        kind: "error",
      });
    } finally {
      setSubmitting(false);
    }
  };

  const doConfirm = async () => {
    if (files.length === 0) return;
    setSubmitting(true);
    try {
      const res = await runImportBatch(false);
      if (res) {
        setStep("done");
        pushToast({
          msg: `导入完成: 写入 ${res.imported} 条, 跳过 ${res.skipped} 条`,
          kind: "success",
        });
        onComplete?.(res);
      }
    } catch (err) {
      pushToast({
        msg: err instanceof Error ? err.message : "导入失败",
        kind: "error",
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title={target === "annotations" ? "导入外部标注" : "导入外部预测"}
      width={560}
    >
      <div className={styles.wizard}>
        <StepBar step={step} />

        {step === "select" && (
          <>
            {target === "predictions" && (
              <div className={styles.formRow}>
                <label htmlFor="pi-format" className={styles.formLabel}>
                  格式
                </label>
                <select
                  id="pi-format"
                  className={styles.input}
                  value={format}
                  onChange={(e) => {
                    setFormat(e.target.value as PredictionImportFormat);
                    setFiles([]);
                    setImageWidth("");
                    setImageHeight("");
                  }}
                >
                  <option value="aap_json">AAP JSON (平台无损)</option>
                  <option value="coco">COCO Detection</option>
                  <option value="yolo">YOLO (zip)</option>
                </select>
              </div>
            )}

            {target === "predictions" && format === "yolo" && (
              <div className={styles.formRow}>
                <label htmlFor="pi-yolo-variant" className={styles.formLabel}>
                  YOLO 变体
                </label>
                <select
                  id="pi-yolo-variant"
                  className={styles.input}
                  value={yoloVariant}
                  onChange={(e) => {
                    setYoloVariant(e.target.value as YoloImportVariant);
                  }}
                >
                  <option value="det">检测 det</option>
                  <option value="obb">旋转框 obb</option>
                  <option value="seg">分割 seg</option>
                </select>
              </div>
            )}

            <div className={styles.formRow}>
              <label className={styles.formLabel}>文件</label>
              <label
                htmlFor="pi-file"
                className={clsx(styles.fileDrop, dragOver && styles.fileDropActive)}
                onDragOver={(e) => {
                  e.preventDefault();
                  setDragOver(true);
                }}
                onDragLeave={() => setDragOver(false)}
                onDrop={handleDrop}
              >
                {files.length > 0 ? (
                  <>
                    <div>已选 {files.length} 个文件</div>
                    <div className={styles.fileName}>{files.map((f) => f.name).join(", ")}</div>
                  </>
                ) : (
                  <div>
                    {format === "yolo"
                      ? "拖入 YOLO zip 文件或点击选择"
                      : "拖入 JSON 文件或点击选择，可多选"}
                  </div>
                )}
                <input
                  id="pi-file"
                  type="file"
                  accept={format === "yolo" ? "application/zip,.zip" : "application/json,.json"}
                  multiple={format !== "yolo"}
                  hidden
                  onChange={(e) => handleFiles(e.target.files)}
                />
              </label>
            </div>

            {format === "coco" && (
              <div className={styles.formRow}>
                <label className={styles.formLabel}>COCO 默认尺寸 (可选)</label>
                <div className={styles.inlineFields}>
                  <input
                    className={styles.input}
                    type="number"
                    min="1"
                    step="1"
                    inputMode="numeric"
                    value={imageWidth}
                    onChange={(e) => setImageWidth(e.target.value)}
                    placeholder="宽度"
                  />
                  <input
                    className={styles.input}
                    type="number"
                    min="1"
                    step="1"
                    inputMode="numeric"
                    value={imageHeight}
                    onChange={(e) => setImageHeight(e.target.value)}
                    placeholder="高度"
                  />
                </div>
              </div>
            )}

            {target === "predictions" && (
              <div className={styles.formRow}>
                <label htmlFor="pi-mv" className={styles.formLabel}>
                  兜底 model_version (可选)
                </label>
                <input
                  id="pi-mv"
                  className={styles.input}
                  value={modelVersion}
                  onChange={(e) => setModelVersion(e.target.value)}
                  placeholder="例如 ext-yolov8-v1"
                />
              </div>
            )}

            <label className={styles.checkboxRow}>
              <input
                type="checkbox"
                checked={overwriteExisting}
                onChange={(e) => setOverwriteExisting(e.target.checked)}
              />
              {target === "annotations"
                ? "替换已有「外部导入」标注 (按 task 维度，不动人工标注)"
                : "替换已有「外部导入」预测 (按 task 维度)"}
            </label>

            <div className={styles.actions}>
              <Button variant="ghost" onClick={close} disabled={submitting}>
                取消
              </Button>
              <Button
                variant="primary"
                onClick={doDryRun}
                disabled={files.length === 0 || submitting}
              >
                {submitting ? "解析中..." : "预览"}
              </Button>
            </div>
          </>
        )}

        {step === "preview" && preview && (
          <>
            <PreviewBox preview={preview} fileResults={fileResults} />
            <div className={styles.actions}>
              <Button variant="ghost" onClick={() => setStep("select")} disabled={submitting}>
                返回修改
              </Button>
              <Button
                variant="primary"
                onClick={doConfirm}
                disabled={submitting || preview.imported === 0}
              >
                {submitting ? "导入中..." : `确认导入 ${preview.imported} 条`}
              </Button>
            </div>
          </>
        )}

        {step === "done" && preview && (
          <>
            <PreviewBox preview={preview} fileResults={fileResults} />
            <div className={styles.actions}>
              <Button variant="primary" onClick={close}>
                完成
              </Button>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}

function StepBar({ step }: { step: WizardStep }) {
  const items: { key: WizardStep; label: string }[] = [
    { key: "select", label: "选择文件" },
    { key: "preview", label: "预览" },
    { key: "done", label: "完成" },
  ];
  const activeIdx = items.findIndex((it) => it.key === step);
  return (
    <div className={styles.stepBar}>
      {items.map((it, i) => (
        <span key={it.key}>
          <span className={clsx(styles.stepDot, i <= activeIdx && styles.stepDotActive)}>
            {i + 1}
          </span>
          <span className={styles.stepLabel}>{it.label}</span>
          {i < items.length - 1 && <span className={styles.stepSeparator}>›</span>}
        </span>
      ))}
    </div>
  );
}

function PreviewBox({
  preview,
  fileResults,
}: {
  preview: PredictionImportResult;
  fileResults: FileImportPreview[];
}) {
  return (
    <div className={styles.previewBox}>
      <div className={styles.previewStats}>
        <div className={styles.previewStat}>
          <span className={styles.previewStatLabel}>将写入</span>
          <span className={styles.previewStatValue}>{preview.imported}</span>
        </div>
        <div className={styles.previewStat}>
          <span className={styles.previewStatLabel}>跳过</span>
          <span className={styles.previewStatValue}>{preview.skipped}</span>
        </div>
        <div className={styles.previewStat}>
          <span className={styles.previewStatLabel}>错误</span>
          <span className={styles.previewStatValue}>{preview.errors.length}</span>
        </div>
      </div>

      {preview.errors.length > 0 && (
        <div className={styles.errors}>
          {preview.errors.slice(0, 50).map((err, idx) => (
            <div key={idx} className={styles.errorRow}>
              <CopyableError
                message={`${JSON.stringify(err.task_match)} · ${err.reason}`}
                className={styles.errorReason}
              />
            </div>
          ))}
          {preview.errors.length > 50 && (
            <div className={styles.errorRow}>
              ... 还有 {preview.errors.length - 50} 条错误未展示
            </div>
          )}
        </div>
      )}

      {fileResults.length > 1 && (
        <div className={styles.fileDetails}>
          {fileResults.map((item, idx) => (
            <div key={`${item.fileName}-${idx}`} className={styles.fileDetailRow}>
              <span className={styles.fileDetailName}>{item.fileName}</span>
              <span>
                写入 {item.result.imported} · 跳过 {item.result.skipped} · 错误{" "}
                {item.result.errors.length}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
