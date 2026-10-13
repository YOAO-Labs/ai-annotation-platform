import { useEffect, useRef, useState, type CSSProperties, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useToastStore } from "@/components/ui/Toast";
import { useCreateDataset, useImportFromConnection } from "@/hooks/useDatasets";
import { useAsyncJob } from "@/hooks/useAsyncJob";
import { useCreateStorageConnection, useStorageConnections } from "@/hooks/useStorageConnections";
import { usePermissions } from "@/hooks/usePermissions";
import {
  StorageConnectionForm,
  type StorageConnectionFormValues,
} from "@/components/connections/StorageConnectionsPanel";
import { datasetsApi } from "@/api/datasets";
import { AxisConventionPicker } from "@/components/datasets/AxisConventionPicker";
import { putWithProgress, runUploadQueue, type QueueItem } from "@/utils/uploadQueue";
import type { DatasetResponse } from "@/api/datasets";
import type { AsyncJob } from "@/api/asyncJobs";
import type { LidarAxisConvention } from "@/pages/Workbench/stages/three-d/geometry/axisConvention";
import styles from "./ImportDatasetWizard.module.css";

type Step = 1 | 2 | 3;
type UploadMode = "files" | "zip" | "connection";

const ZIP_MAX_BYTES = 200 * 1024 * 1024;

interface ZipResult {
  added: number;
  skipped: number;
  errors: Array<{ name: string; error: string }>;
  total_in_zip: number;
}

interface Props {
  open: boolean;
  onClose: () => void;
  /** 若给定，则跳过新建步骤，直接向已有数据集追加文件。 */
  datasetId?: string;
  datasetName?: string;
  onUploaded?: (datasetId: string, addedCount: number) => void;
}

const DATA_TYPES: Array<{ key: string; label: string }> = [
  { key: "image", label: "图像" },
  { key: "video", label: "视频" },
  { key: "point_cloud", label: "3D 点云" },
  { key: "multimodal", label: "多模态" },
  { key: "other", label: "其他" },
];

const STEP_LABELS: Record<Step, string> = {
  1: "选择来源",
  2: "基本信息",
  3: "导入完成",
};

/** 取路径末段作为数据集名候选（如 `a/b/dataset-A/` → `dataset-A`）。 */
function lastPathSegment(path: string): string {
  return path.split("/").filter(Boolean).pop() ?? "";
}

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function ProgressFill({ progress, color }: { progress: number; color: string }) {
  const ref = useElementStyle<HTMLDivElement>({
    "--progress": `${progress}%`,
    "--progress-color": color,
  } as CSSProperties);
  return <div ref={ref} className={styles.progressFill} />;
}

export function ImportDatasetWizard({ open, onClose, datasetId, datasetName, onUploaded }: Props) {
  const navigate = useNavigate();
  const pushToast = useToastStore((s) => s.push);
  const createDataset = useCreateDataset();
  const importFromConnection = useImportFromConnection();

  const skipCreate = !!datasetId;
  const [step, setStep] = useState<Step>(1);
  const [mode, setMode] = useState<UploadMode>("files");
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [dataType, setDataType] = useState("image");
  const [axisConvention, setAxisConvention] = useState<LidarAxisConvention>("iso_8855");
  const [isTemporal, setIsTemporal] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [zipProgress, setZipProgress] = useState(0);
  const [zipResult, setZipResult] = useState<ZipResult | null>(null);
  const [zipError, setZipError] = useState<string | null>(null);
  const [connectionId, setConnectionId] = useState("");
  const [sourcePath, setSourcePath] = useState("");
  const [recursive, setRecursive] = useState(true);
  const [includeGlobs, setIncludeGlobs] = useState("");
  const [connectionJobId, setConnectionJobId] = useState<string | null>(null);
  const [connectionImportError, setConnectionImportError] = useState<string | null>(null);
  const [created, setCreated] = useState<DatasetResponse | null>(null);
  const [items, setItems] = useState<Map<string, QueueItem>>(new Map());
  const [running, setRunning] = useState(false);
  const tickRef = useRef(0);
  const [, force] = useState(0);

  useEffect(() => {
    if (!open) {
      // reset
      setStep(1);
      setMode("files");
      setName("");
      setNameTouched(false);
      setDescription("");
      setDataType("image");
      setAxisConvention("iso_8855");
      setIsTemporal(false);
      setFiles([]);
      setZipFile(null);
      setZipProgress(0);
      setZipResult(null);
      setZipError(null);
      setConnectionId("");
      setSourcePath("");
      setRecursive(true);
      setIncludeGlobs("");
      setConnectionJobId(null);
      setConnectionImportError(null);
      setCreated(null);
      setItems(new Map());
      setRunning(false);
      createDataset.reset();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const trimmedName = name.trim();
  const nameValid = skipCreate || (trimmedName.length >= 2 && trimmedName.length <= 60);

  const targetDatasetId = datasetId || created?.id;

  const handleSetName = (v: string) => {
    setNameTouched(true);
    setName(v);
  };

  const handleSetSourcePath = (v: string) => {
    setSourcePath(v);
    // 连接器模式、用户未手改过名字时，用路径末段自动填充数据集名（仍可在「基本信息」步改）
    if (!skipCreate && !nameTouched && mode === "connection") {
      const seg = lastPathSegment(v);
      if (seg) setName(seg);
    }
  };

  const handleAddFiles = (incoming: FileList | File[]) => {
    const list = Array.from(incoming).filter((f) => f.size > 0);
    setFiles((prev) => {
      const seen = new Set(prev.map((file) => `${file.name}::${file.size}`));
      const merged = [...prev];
      for (const file of list) {
        const key = `${file.name}::${file.size}`;
        if (!seen.has(key)) {
          seen.add(key);
          merged.push(file);
        }
      }
      return merged;
    });
  };

  const handleDrop = (e: DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer?.files?.length) handleAddFiles(e.dataTransfer.files);
  };

  const ensureDataset = async (): Promise<string | null> => {
    if (targetDatasetId) return targetDatasetId;
    try {
      const dsResp = await createDataset.mutateAsync({
        name: trimmedName,
        description: description.trim() || undefined,
        data_type: dataType,
        axis_convention: dataType === "point_cloud" ? axisConvention : undefined,
        is_temporal: isTemporal || undefined,
      });
      setCreated(dsResp);
      return dsResp.id;
    } catch (err) {
      pushToast({
        msg: "创建数据集失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
      return null;
    }
  };

  const startFilesUpload = async () => {
    if (running || !files.length) return;
    const dsId = await ensureDataset();
    if (!dsId) return;

    const map = new Map<string, QueueItem>();
    files.forEach((_, i) => {
      map.set(`${i}`, {
        id: `${i}`,
        status: "pending",
        progress: 0,
      });
    });
    setItems(map);
    setStep(3);
    setRunning(true);

    const tasks = files.map((file, i) => ({
      id: `${i}`,
      worker: async (_signal: { aborted: boolean }, onProgress: (pct: number) => void) => {
        const init = await datasetsApi.uploadInit(dsId, {
          file_name: file.name,
          content_type: file.type || "application/octet-stream",
        });
        await putWithProgress(init.upload_url, file, onProgress);
        await datasetsApi.uploadComplete(dsId, init.item_id);
        return init.item_id;
      },
    }));

    await runUploadQueue(tasks, map, {
      concurrency: 3,
      onUpdate: () => {
        tickRef.current += 1;
        force((n) => n + 1);
      },
    });

    setRunning(false);
    const succeeded = Array.from(map.values()).filter((it) => it.status === "done").length;
    onUploaded?.(dsId, succeeded);
    pushToast({
      msg: `上传完成：成功 ${succeeded} / ${files.length}`,
      kind: succeeded === files.length ? "success" : succeeded === 0 ? "error" : "warning",
    });
  };

  const startZipUpload = async () => {
    if (running || !zipFile) return;
    const dsId = await ensureDataset();
    if (!dsId) return;

    setStep(3);
    setRunning(true);
    setZipProgress(0);
    setZipResult(null);
    setZipError(null);

    try {
      const res = await datasetsApi.uploadZip(dsId, zipFile, (pct) => setZipProgress(pct));
      setZipResult(res);
      onUploaded?.(dsId, res.added);
      pushToast({
        msg: `ZIP 解包完成：新增 ${res.added} 个文件`,
        sub: res.errors.length ? `${res.errors.length} 个失败` : undefined,
        kind:
          res.errors.length === 0 && res.added > 0
            ? "success"
            : res.added === 0
              ? "error"
              : "warning",
      });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setZipError(msg);
      pushToast({ msg: "ZIP 上传失败", sub: msg, kind: "error" });
    } finally {
      setRunning(false);
    }
  };

  const startConnectionImport = async () => {
    if (running || !connectionId) return;
    const dsId = await ensureDataset();
    if (!dsId) return;

    setStep(3);
    setRunning(true);
    setConnectionJobId(null);
    setConnectionImportError(null);

    const globs = includeGlobs
      .split(",")
      .map((pattern) => pattern.trim())
      .filter(Boolean);

    try {
      const res = await importFromConnection.mutateAsync({
        datasetId: dsId,
        payload: {
          connection_id: connectionId,
          source_path: sourcePath.trim(),
          recursive,
          include_globs: globs,
        },
      });
      setConnectionJobId(res.job_id);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setConnectionImportError(msg);
      setRunning(false);
      pushToast({ msg: "连接器导入提交失败", sub: msg, kind: "error" });
    }
  };

  // 提交（开始上传/导入）所在的步：追加模式在「来源」步直接提交；新建模式在「基本信息」步提交。
  const isSubmitStep = skipCreate ? step === 1 : step === 2;

  const startByMode = () => {
    if (mode === "files") startFilesUpload();
    else if (mode === "zip") startZipUpload();
    else startConnectionImport();
  };

  const goNextOrSubmit = () => {
    if (step === 1 && !skipCreate) setStep(2);
    else if (isSubmitStep) startByMode();
  };

  const titleSuffix = datasetName ? ` · ${datasetName}` : "";

  const stepNums: Step[] = skipCreate ? [1, 3] : [1, 2, 3];

  return (
    <Modal open={open} onClose={onClose} title={`导入数据集${titleSuffix}`} width={640}>
      <Stepper current={step} steps={stepNums} />

      {step === 2 && !skipCreate && (
        <Step1
          name={name}
          description={description}
          dataType={dataType}
          setName={handleSetName}
          setDescription={setDescription}
          setDataType={(v) => {
            setDataType(v);
            // 切换数据类型时重置时序声明：时序开关仅在 image/video/point_cloud 显示，
            // 切到 other/multimodal 后若仍残留 true 会让后端 scene 校验意外 422。
            setIsTemporal(false);
          }}
          axisConvention={axisConvention}
          setAxisConvention={setAxisConvention}
          isTemporal={isTemporal}
          setIsTemporal={setIsTemporal}
          nameValid={nameValid}
        />
      )}

      {step === 1 && (
        <Step2
          mode={mode}
          setMode={(m) => {
            setMode(m);
            // 切换模式时清掉对侧选择
            if (m !== "files") setFiles([]);
            if (m !== "zip") setZipFile(null);
            if (m !== "connection") {
              setConnectionId("");
              setSourcePath("");
              setIncludeGlobs("");
              setConnectionJobId(null);
              setConnectionImportError(null);
            }
          }}
          files={files}
          zipFile={zipFile}
          connectionId={connectionId}
          sourcePath={sourcePath}
          recursive={recursive}
          includeGlobs={includeGlobs}
          onAddFiles={handleAddFiles}
          onSetZip={(f) => setZipFile(f)}
          onSetConnectionId={setConnectionId}
          onSetSourcePath={handleSetSourcePath}
          onSetRecursive={setRecursive}
          onSetIncludeGlobs={setIncludeGlobs}
          onDrop={handleDrop}
          onRemove={(idx) => setFiles((arr) => arr.filter((_, i) => i !== idx))}
        />
      )}

      {step === 3 &&
        (mode === "files" ? (
          <Step3
            files={files}
            items={items}
            running={running}
            onClose={onClose}
            onView={() => {
              const id = targetDatasetId;
              onClose();
              if (id) navigate(`/datasets`);
            }}
          />
        ) : mode === "zip" ? (
          <Step3Zip
            zipFile={zipFile!}
            progress={zipProgress}
            running={running}
            result={zipResult}
            error={zipError}
            onClose={onClose}
            onView={() => {
              const id = targetDatasetId;
              onClose();
              if (id) navigate(`/datasets`);
            }}
          />
        ) : (
          <Step3Connection
            jobId={connectionJobId}
            submitError={connectionImportError}
            running={running}
            onSettled={(job) => {
              setRunning(false);
              const added = connectionImportAdded(job);
              if (targetDatasetId) onUploaded?.(targetDatasetId, added);
            }}
            onClose={onClose}
            onView={() => {
              const id = targetDatasetId;
              onClose();
              if (id) navigate(`/datasets`);
            }}
          />
        ))}

      {step !== 3 && (
        <Footer
          isSubmitStep={isSubmitStep}
          showPrev={!skipCreate && step === 2}
          mode={mode}
          canNext={
            step === 1
              ? mode === "files"
                ? files.length > 0
                : mode === "zip"
                  ? !!zipFile
                  : !!connectionId
              : nameValid
          }
          loading={createDataset.isPending || importFromConnection.isPending}
          onCancel={onClose}
          onPrev={() => setStep(1)}
          onNext={goNextOrSubmit}
        />
      )}
    </Modal>
  );
}

// ── Stepper ──────────────────────────────────────────────────────────────────

function Stepper({ current, steps }: { current: Step; steps: Step[] }) {
  return (
    <div className={styles.stepper}>
      {steps.map((n, i) => {
        const active = n === current;
        const done = n < current;
        const last = i === steps.length - 1;
        return (
          <div key={n} className={cx(styles.stepItem, last && styles.stepItemLast)}>
            <div
              className={cx(
                styles.stepDot,
                active && styles.stepDotActive,
                done && styles.stepDotDone,
              )}
            >
              {done ? <Icon name="check" size={12} /> : n}
            </div>
            <span
              className={cx(
                styles.stepLabel,
                active && styles.stepLabelActive,
                done && styles.stepLabelDone,
              )}
            >
              {STEP_LABELS[n]}
            </span>
            {!last && <div className={cx(styles.stepLine, n < current && styles.stepLineDone)} />}
          </div>
        );
      })}
    </div>
  );
}

// ── Step 1 ───────────────────────────────────────────────────────────────────

function Step1({
  name,
  description,
  dataType,
  setName,
  setDescription,
  setDataType,
  axisConvention,
  setAxisConvention,
  isTemporal,
  setIsTemporal,
  nameValid,
}: {
  name: string;
  description: string;
  dataType: string;
  setName: (v: string) => void;
  setDescription: (v: string) => void;
  setDataType: (v: string) => void;
  axisConvention: LidarAxisConvention;
  setAxisConvention: (v: LidarAxisConvention) => void;
  isTemporal: boolean;
  setIsTemporal: (v: boolean) => void;
  nameValid: boolean;
}) {
  return (
    <div className={styles.stackLarge}>
      <div>
        <label className={styles.label}>数据集名称</label>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="如：商品检测训练集 v1"
          maxLength={60}
          className={cx(styles.input, !nameValid && styles.inputInvalid)}
        />
      </div>
      <div>
        <label className={styles.label}>描述（可选）</label>
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={2}
          placeholder="简要说明数据来源、采集场景等"
          className={styles.textarea}
        />
      </div>
      <div>
        <label className={styles.label}>数据类型</label>
        <div className={styles.segmented}>
          {DATA_TYPES.map((t) => {
            const active = dataType === t.key;
            return (
              <button
                key={t.key}
                type="button"
                onClick={() => setDataType(t.key)}
                className={cx(styles.segmentButton, active && styles.segmentButtonActive)}
              >
                {t.label}
              </button>
            );
          })}
        </div>
      </div>
      {dataType === "point_cloud" && (
        <div>
          <label className={styles.label}>LiDAR 坐标系约定</label>
          <AxisConventionPicker value={axisConvention} onChange={setAxisConvention} />
        </div>
      )}
      {(dataType === "image" || dataType === "video" || dataType === "point_cloud") && (
        <div>
          <label className={styles.checkField}>
            <input
              type="checkbox"
              checked={isTemporal}
              onChange={(e) => setIsTemporal(e.target.checked)}
            />
            <span>声明为时序数据集（scene）</span>
          </label>
          <div className={styles.mutedSmall}>
            勾选后，上传 / 入库完成时若未识别出任何 scene 会失败并提示检查目录结构；用于 scene
            模式项目。ZIP 上传会自动识别 scene——点云按 <code>lidar/ camera/ calib/</code> 布局，图片
            / 视频按帧序列或多场景子目录。
          </div>
        </div>
      )}
    </div>
  );
}

// ── Step 2 ───────────────────────────────────────────────────────────────────

function Step2({
  mode,
  setMode,
  files,
  zipFile,
  connectionId,
  sourcePath,
  recursive,
  includeGlobs,
  onAddFiles,
  onSetZip,
  onSetConnectionId,
  onSetSourcePath,
  onSetRecursive,
  onSetIncludeGlobs,
  onDrop,
  onRemove,
}: {
  mode: UploadMode;
  setMode: (m: UploadMode) => void;
  files: File[];
  zipFile: File | null;
  connectionId: string;
  sourcePath: string;
  recursive: boolean;
  includeGlobs: string;
  onAddFiles: (files: FileList | File[]) => void;
  onSetZip: (f: File | null) => void;
  onSetConnectionId: (id: string) => void;
  onSetSourcePath: (path: string) => void;
  onSetRecursive: (value: boolean) => void;
  onSetIncludeGlobs: (value: string) => void;
  onDrop: (e: DragEvent<HTMLDivElement>) => void;
  onRemove: (idx: number) => void;
}) {
  const filesInputRef = useRef<HTMLInputElement>(null);
  const zipInputRef = useRef<HTMLInputElement>(null);
  const [hover, setHover] = useState(false);
  // 行内校验错误(替代原原生 alert 提示):紧贴选择区展示,选中合规文件即清除。
  const [zipError, setZipError] = useState<string | null>(null);
  const totalSize = files.reduce((sum, f) => sum + f.size, 0);

  const handleZipPick = (f: File | null) => {
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) {
      setZipError("请选择 .zip 文件");
      return;
    }
    if (f.size > ZIP_MAX_BYTES) {
      setZipError(`ZIP 包不能超过 ${ZIP_MAX_BYTES / 1024 / 1024}MB`);
      return;
    }
    setZipError(null);
    onSetZip(f);
  };

  return (
    <div className={styles.stackMedium}>
      {/* mode toggle */}
      <div className={styles.modeTabs}>
        {(
          [
            { key: "files", label: "多文件" },
            { key: "zip", label: "ZIP 包 (≤200MB)" },
            { key: "connection", label: "连接器导入" },
          ] as const
        ).map((opt) => {
          const active = mode === opt.key;
          return (
            <button
              key={opt.key}
              type="button"
              onClick={() => setMode(opt.key)}
              className={cx(
                styles.segmentButton,
                styles.modeButton,
                active && styles.segmentButtonActive,
              )}
            >
              {opt.label}
            </button>
          );
        })}
      </div>

      {mode === "files" && (
        <>
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setHover(true);
            }}
            onDragLeave={() => setHover(false)}
            onDrop={(e) => {
              setHover(false);
              onDrop(e);
            }}
            onClick={() => filesInputRef.current?.click()}
            className={cx(styles.dropZone, hover && styles.dropZoneHover)}
          >
            <Icon name="upload" size={22} className={styles.uploadIcon} />
            <div className={styles.dropTitle}>
              拖拽文件到此处，或<span className={styles.accentText}> 点击选择</span>
            </div>
            <div className={styles.dropHint}>支持图像 / 视频 / 任意二进制；单文件 ≤ 5GB</div>
            <input
              ref={filesInputRef}
              type="file"
              multiple
              className={styles.hiddenInput}
              onChange={(e) => {
                if (e.target.files) onAddFiles(e.target.files);
                e.target.value = "";
              }}
            />
          </div>

          {files.length > 0 && (
            <div className={styles.filePanel}>
              <div className={styles.filePanelHeader}>
                <span>已选 {files.length} 个文件</span>
                <span>{formatBytes(totalSize)}</span>
              </div>
              {files.map((f, i) => (
                <div key={`${f.name}-${i}`} className={styles.fileRow}>
                  <Icon name={iconForFile(f)} size={12} className={styles.mutedIcon} />
                  <span className={styles.fileName}>{f.name}</span>
                  <span className={styles.mutedSmall}>{formatBytes(f.size)}</span>
                  <button
                    type="button"
                    onClick={() => onRemove(i)}
                    aria-label="移除"
                    className={styles.iconButton}
                  >
                    <Icon name="x" size={12} />
                  </button>
                </div>
              ))}
            </div>
          )}
        </>
      )}

      {mode === "zip" && (
        <>
          <div
            onClick={() => zipInputRef.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setHover(true);
            }}
            onDragLeave={() => setHover(false)}
            onDrop={(e) => {
              e.preventDefault();
              setHover(false);
              const f = e.dataTransfer?.files?.[0];
              handleZipPick(f ?? null);
            }}
            className={cx(styles.dropZone, hover && styles.dropZoneHover)}
          >
            <Icon name="upload" size={22} className={styles.uploadIcon} />
            <div className={styles.dropTitle}>
              拖入或<span className={styles.accentText}> 点击选择</span> ZIP 包
            </div>
            <div className={styles.dropHint}>
              整包 ≤ 200MB；包内文件数 ≤ 5000；自动跳过 __MACOSX/ 与隐藏文件
            </div>
            <input
              ref={zipInputRef}
              type="file"
              accept=".zip,application/zip"
              className={styles.hiddenInput}
              onChange={(e) => {
                handleZipPick(e.target.files?.[0] ?? null);
                e.target.value = "";
              }}
            />
          </div>

          {zipError && (
            <p role="alert" className="mt-2 text-xs font-medium text-status-danger">
              {zipError}
            </p>
          )}

          {zipFile && (
            <div className={styles.zipFileRow}>
              <Icon name="folder" size={14} className={styles.mutedIcon} />
              <span className={styles.fileName}>{zipFile.name}</span>
              <span className={styles.mutedMedium}>{formatBytes(zipFile.size)}</span>
              <button
                type="button"
                onClick={() => onSetZip(null)}
                aria-label="移除"
                className={styles.iconButton}
              >
                <Icon name="x" size={12} />
              </button>
            </div>
          )}
        </>
      )}

      {mode === "connection" && (
        <ConnectionImportSource
          connectionId={connectionId}
          sourcePath={sourcePath}
          recursive={recursive}
          includeGlobs={includeGlobs}
          onSetConnectionId={onSetConnectionId}
          onSetSourcePath={onSetSourcePath}
          onSetRecursive={onSetRecursive}
          onSetIncludeGlobs={onSetIncludeGlobs}
        />
      )}
    </div>
  );
}

function ConnectionImportSource({
  connectionId,
  sourcePath,
  recursive,
  includeGlobs,
  onSetConnectionId,
  onSetSourcePath,
  onSetRecursive,
  onSetIncludeGlobs,
}: {
  connectionId: string;
  sourcePath: string;
  recursive: boolean;
  includeGlobs: string;
  onSetConnectionId: (id: string) => void;
  onSetSourcePath: (path: string) => void;
  onSetRecursive: (value: boolean) => void;
  onSetIncludeGlobs: (value: string) => void;
}) {
  const { role } = usePermissions();
  const isSuper = role === "super_admin";
  const canManage = isSuper || role === "project_admin";
  const pushToast = useToastStore((s) => s.push);
  const { data: connections = [], isLoading } = useStorageConnections();
  const createConnection = useCreateStorageConnection();
  const [showCreate, setShowCreate] = useState(false);

  const handleCreate = async (values: StorageConnectionFormValues) => {
    const conn = await createConnection.mutateAsync({
      name: values.name,
      kind: values.kind,
      scope: values.scope,
      config: values.config,
      secret: values.secret ?? {},
    });
    onSetConnectionId(conn.id);
    setShowCreate(false);
    pushToast({ msg: "连接器已创建" });
  };

  return (
    <div className={styles.connectionSource}>
      <div className={styles.connectionGrid}>
        <label className={styles.field}>
          <span>连接器</span>
          <select
            value={connectionId}
            onChange={(event) => onSetConnectionId(event.target.value)}
            className={styles.input}
            disabled={isLoading || connections.length === 0}
          >
            <option value="">
              {isLoading ? "加载中..." : connections.length ? "选择连接器" : "暂无连接器"}
            </option>
            {connections.map((conn) => (
              <option key={conn.id} value={conn.id}>
                {conn.name} · {conn.kind.toUpperCase()} ·{" "}
                {conn.scope === "global" ? "全局" : "个人"}
              </option>
            ))}
          </select>
        </label>
        <label className={styles.field}>
          <span>Source path</span>
          <input
            value={sourcePath}
            onChange={(event) => onSetSourcePath(event.target.value)}
            className={styles.input}
            placeholder="batch-a/"
          />
        </label>
        <label className={styles.fieldWide}>
          <span>Include globs</span>
          <input
            value={includeGlobs}
            onChange={(event) => onSetIncludeGlobs(event.target.value)}
            className={styles.input}
            placeholder="*.jpg, *.png"
          />
        </label>
        <label className={styles.checkField}>
          <input
            type="checkbox"
            checked={recursive}
            onChange={(event) => onSetRecursive(event.target.checked)}
          />
          <span>递归扫描</span>
        </label>
      </div>

      {canManage && (
        <div className={styles.inlineCreateHeader}>
          <Button size="sm" onClick={() => setShowCreate((value) => !value)}>
            <Icon name={showCreate ? "x" : "plus"} size={12} />
            {showCreate ? "收起新建" : "新建连接器"}
          </Button>
        </div>
      )}

      {canManage && showCreate && (
        <StorageConnectionForm
          compact
          isSuper={isSuper}
          submitLabel="创建并选择"
          submitting={createConnection.isPending}
          onSubmit={handleCreate}
          onCancel={() => setShowCreate(false)}
        />
      )}
    </div>
  );
}

// ── Step 3 ───────────────────────────────────────────────────────────────────

function Step3({
  files,
  items,
  running,
  onClose,
  onView,
}: {
  files: File[];
  items: Map<string, QueueItem>;
  running: boolean;
  onClose: () => void;
  onView: () => void;
}) {
  const arr = files.map((f, i) => ({ file: f, item: items.get(`${i}`) }));
  const done = arr.filter((x) => x.item?.status === "done").length;
  const failed = arr.filter((x) => x.item?.status === "error").length;
  const overall =
    arr.length === 0
      ? 0
      : Math.round(arr.reduce((s, x) => s + (x.item?.progress ?? 0), 0) / arr.length);

  return (
    <div className={styles.stackMedium}>
      <div className={styles.summaryPanel}>
        <span>
          总进度 <strong>{overall}%</strong> · 成功 {done} / 失败 {failed} / 共 {arr.length}
        </span>
        <span className={running ? styles.runningText : styles.mutedMedium}>
          {running ? "上传中…" : "已完成"}
        </span>
      </div>

      <div className={styles.progressPanel}>
        {arr.map(({ file, item }, i) => (
          <div key={i} className={styles.progressItem}>
            <div className={styles.progressFileHeader}>
              <Icon
                name={
                  item?.status === "done"
                    ? "check"
                    : item?.status === "error"
                      ? "warning"
                      : iconForFile(file)
                }
                size={12}
                className={
                  item?.status === "done"
                    ? styles.successIcon
                    : item?.status === "error"
                      ? styles.dangerIcon
                      : styles.mutedIcon
                }
              />
              <span className={styles.fileName}>{file.name}</span>
              <span className={styles.mutedSmall}>
                {item?.status === "error" ? "失败" : `${Math.round(item?.progress ?? 0)}%`}
              </span>
            </div>
            <div className={styles.progressTrack}>
              <ProgressFill
                progress={item?.progress ?? 0}
                color={
                  item?.status === "error"
                    ? "var(--sc-destructive)"
                    : item?.status === "done"
                      ? "var(--sc-positive)"
                      : "var(--sc-brand)"
                }
              />
            </div>
            {item?.error && <div className={styles.errorMessage}>{item.error}</div>}
          </div>
        ))}
      </div>

      <div className={styles.actions}>
        <Button onClick={onClose}>关闭</Button>
        <Button variant="primary" onClick={onView} disabled={running}>
          查看数据集
        </Button>
      </div>
    </div>
  );
}

// ── Footer ───────────────────────────────────────────────────────────────────

function Footer({
  isSubmitStep,
  showPrev,
  mode,
  canNext,
  loading,
  onCancel,
  onPrev,
  onNext,
}: {
  isSubmitStep: boolean;
  showPrev: boolean;
  mode: UploadMode;
  canNext: boolean;
  loading: boolean;
  onCancel: () => void;
  onPrev: () => void;
  onNext: () => void;
}) {
  const submitLabel = mode === "zip" ? "上传 ZIP" : mode === "connection" ? "开始导入" : "开始上传";
  return (
    <div className={styles.footer}>
      <div>
        {showPrev && (
          <Button onClick={onPrev}>
            <Icon name="chevLeft" size={12} /> 上一步
          </Button>
        )}
      </div>
      <div className={styles.footerActions}>
        <Button onClick={onCancel}>取消</Button>
        <Button variant="primary" onClick={onNext} disabled={!canNext || loading}>
          {isSubmitStep ? (loading ? "处理中…" : submitLabel) : "下一步"}
          {!isSubmitStep && <Icon name="chevRight" size={12} />}
        </Button>
      </div>
    </div>
  );
}

// ── Step 3 (ZIP) ─────────────────────────────────────────────────────────────

function Step3Zip({
  zipFile,
  progress,
  running,
  result,
  error,
  onClose,
  onView,
}: {
  zipFile: File;
  progress: number;
  running: boolean;
  result: ZipResult | null;
  error: string | null;
  onClose: () => void;
  onView: () => void;
}) {
  return (
    <div className={styles.stackMedium}>
      <div className={styles.zipFileRow}>
        <Icon name="folder" size={14} className={styles.mutedIcon} />
        <span className={styles.fileName}>{zipFile.name}</span>
        <span className={styles.mutedMedium}>{formatBytes(zipFile.size)}</span>
      </div>

      <div>
        <div className={styles.zipProgressLabel}>
          <span>
            {running
              ? "上传中…（服务端解压通常在 0% 跳到 100% 后等待几秒）"
              : result
                ? "解包完成"
                : error
                  ? "失败"
                  : "等待"}
          </span>
          <span>{Math.round(progress)}%</span>
        </div>
        <div className={styles.zipProgressTrack}>
          <ProgressFill
            progress={progress}
            color={
              error ? "var(--sc-destructive)" : result ? "var(--sc-positive)" : "var(--sc-brand)"
            }
          />
        </div>
      </div>

      {result && (
        <div className={styles.zipSummary}>
          <div>
            <strong className={styles.successText}>新增 {result.added}</strong> 个文件 ·{" "}
            <span className={styles.mutedMedium}>
              ZIP 内共 {result.total_in_zip} · 跳过 {result.skipped} · 失败 {result.errors.length}
            </span>
          </div>
          {result.errors.length > 0 && (
            <details className={styles.errorDetails}>
              <summary className={styles.errorSummary}>
                查看 {result.errors.length} 条失败明细
              </summary>
              <div className={styles.errorList}>
                {result.errors.slice(0, 50).map((e, i) => (
                  <div key={i} className={styles.errorRow}>
                    <span className="mono">{e.name}</span> — {e.error}
                  </div>
                ))}
                {result.errors.length > 50 && (
                  <div className={styles.omittedRow}>
                    …其余 {result.errors.length - 50} 条已省略
                  </div>
                )}
              </div>
            </details>
          )}
        </div>
      )}

      {error && <div className={styles.zipError}>{error}</div>}

      <div className={styles.actions}>
        <Button onClick={onClose}>关闭</Button>
        <Button variant="primary" onClick={onView} disabled={running}>
          查看数据集
        </Button>
      </div>
    </div>
  );
}

// ── Step 3 (Connection) ─────────────────────────────────────────────────────

function Step3Connection({
  jobId,
  submitError,
  running,
  onSettled,
  onClose,
  onView,
}: {
  jobId: string | null;
  submitError: string | null;
  running: boolean;
  onSettled: (job: AsyncJob) => void;
  onClose: () => void;
  onView: () => void;
}) {
  const pushToast = useToastStore((s) => s.push);
  const { data: job, isLoading } = useAsyncJob(jobId, !!jobId);
  const settledRef = useRef<string | null>(null);
  const terminal =
    job?.status === "completed" || job?.status === "failed" || job?.status === "cancelled";

  useEffect(() => {
    if (!job || !terminal || settledRef.current === job.id) return;
    settledRef.current = job.id;
    onSettled(job);
    if (job.status === "completed") {
      const added = connectionImportAdded(job);
      pushToast({ msg: `连接器导入完成：新增 ${added} 个文件`, kind: "success" });
    } else {
      pushToast({
        msg: job.status === "cancelled" ? "连接器导入已取消" : "连接器导入失败",
        sub: job.error_message ?? undefined,
        kind: job.status === "cancelled" ? "warning" : "error",
      });
    }
  }, [job, onSettled, pushToast, terminal]);

  const progress = job?.progress_pct ?? (jobId ? 0 : running ? 5 : 0);
  const result = job?.result ?? {};
  const added = readJobNumber(result.added ?? result.imported);
  const skipped = readJobNumber(result.skipped);
  const errors = readJobNumber(result.error_count);
  const total = readJobNumber(result.total);
  const busy = running && !terminal && !submitError;

  return (
    <div className={styles.stackMedium}>
      <div className={styles.summaryPanel}>
        <span>
          任务进度 <strong>{Math.round(progress)}%</strong>
          {job && <> · {jobStatusLabel(job.status)}</>}
        </span>
        <span className={busy ? styles.runningText : styles.mutedMedium}>
          {submitError ? "提交失败" : busy ? "导入中…" : terminal ? "已结束" : "等待"}
        </span>
      </div>

      <div className={styles.zipProgressTrack}>
        <ProgressFill
          progress={progress}
          color={
            submitError || job?.status === "failed"
              ? "var(--sc-destructive)"
              : job?.status === "completed"
                ? "var(--sc-positive)"
                : "var(--sc-brand)"
          }
        />
      </div>

      {!submitError && !job && (
        <div className={styles.connectionJobHint}>
          {isLoading || jobId ? "正在读取导入任务..." : "正在提交导入任务..."}
        </div>
      )}

      {job && (
        <div className={styles.zipSummary}>
          <div>
            <strong
              className={job.status === "completed" ? styles.successText : styles.runningText}
            >
              新增 {added}
            </strong>{" "}
            个文件 ·{" "}
            <span className={styles.mutedMedium}>
              共 {total} · 跳过 {skipped} · 失败 {errors}
            </span>
          </div>
          {job.error_message && <div className={styles.errorMessage}>{job.error_message}</div>}
          {Array.isArray(result.errors) && result.errors.length > 0 && (
            <details className={styles.errorDetails}>
              <summary className={styles.errorSummary}>
                查看 {result.errors.length} 条失败明细
              </summary>
              <div className={styles.errorList}>
                {result.errors.slice(0, 50).map((entry, index) => (
                  <div key={index} className={styles.errorRow}>
                    {formatJobError(entry)}
                  </div>
                ))}
              </div>
            </details>
          )}
        </div>
      )}

      {submitError && <div className={styles.zipError}>{submitError}</div>}

      <div className={styles.actions}>
        <Button onClick={onClose}>关闭</Button>
        <Button variant="primary" onClick={onView} disabled={busy}>
          查看数据集
        </Button>
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function iconForFile(f: File) {
  if (f.type.startsWith("image/")) return "image" as const;
  if (f.type.startsWith("video/")) return "video" as const;
  return "layers" as const;
}

function readJobNumber(value: unknown): number {
  return typeof value === "number" ? value : 0;
}

function connectionImportAdded(job: AsyncJob): number {
  return readJobNumber(job.result?.added ?? job.result?.imported);
}

function jobStatusLabel(status: string): string {
  if (status === "pending") return "排队中";
  if (status === "running") return "运行中";
  if (status === "completed") return "完成";
  if (status === "failed") return "失败";
  if (status === "cancelled") return "已取消";
  return status;
}

function formatJobError(entry: unknown): string {
  if (!entry || typeof entry !== "object") return String(entry);
  const data = entry as Record<string, unknown>;
  const path = typeof data.path === "string" ? data.path : data.name;
  const error = typeof data.error === "string" ? data.error : data.message;
  return [path, error].filter(Boolean).join(" - ");
}
