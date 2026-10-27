import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useState } from "react";

import {
  maskFormatsApi,
  type MaskFormatDescriptor,
  type MaskFormatImportPreflight,
} from "@/api/maskFormats";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";

async function sha256(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join(
    "",
  );
}

async function uploadFile(url: string, file: File): Promise<void> {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (!response.ok) throw new Error(`上传失败 (${response.status})`);
}

export function MaskFormatImportWizard({
  open,
  projectId,
  onClose,
  onQueued,
}: {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onQueued?: () => void;
}) {
  const pushToast = useToastStore((state) => state.push);
  const [formats, setFormats] = useState<MaskFormatDescriptor[]>([]);
  const [formatId, setFormatId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [preflight, setPreflight] = useState<MaskFormatImportPreflight | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [lossyConfirmed, setLossyConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [queued, setQueued] = useState(false);
  const [staged, setStaged] = useState<{ objectKey: string; digest: string } | null>(null);
  const [sparseGapPolicy, setSparseGapPolicy] = useState<"outside_gaps" | "nearest_hold">(
    "outside_gaps",
  );
  const [frameBase, setFrameBase] = useState<0 | 1>(0);

  const availableFormats = useMemo(
    () =>
      formats.filter(
        (format) =>
          format.import_capability.supported &&
          format.import_capability.verified &&
          format.import_capability.enabled_for_ui,
      ),
    [formats],
  );

  useEffect(() => {
    const defaultFrameBase = formats.find((format) => format.format_id === formatId)?.option_schema
      .frame_base;
    if (defaultFrameBase && typeof defaultFrameBase === "object" && "default" in defaultFrameBase) {
      setFrameBase(defaultFrameBase.default === 1 ? 1 : 0);
    }
  }, [formatId, formats]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    maskFormatsApi
      .list(projectId)
      .then((rows) => {
        if (!active) return;
        const available = rows.filter(
          (format) =>
            format.import_capability.supported &&
            format.import_capability.verified &&
            format.import_capability.enabled_for_ui,
        );
        setFormats(rows);
        setFormatId((current) => current || available[0]?.format_id || "");
      })
      .catch((error) => {
        pushToast({
          msg: "无法加载标注导入格式",
          sub: error instanceof Error ? error.message : undefined,
          kind: "error",
        });
      });
    return () => {
      active = false;
    };
  }, [open, projectId, pushToast]);

  const resetPlan = () => {
    setPreflight(null);
    setMapping({});
    setLossyConfirmed(false);
    setQueued(false);
  };

  const runPreflight = async () => {
    if (!file || !formatId) return;
    setBusy(true);
    try {
      let uploaded = staged;
      if (!uploaded) {
        const [upload, digest] = await Promise.all([
          maskFormatsApi.initImportUpload(projectId, file),
          sha256(file),
        ]);
        await uploadFile(upload.upload_url, file);
        uploaded = { objectKey: upload.object_key, digest };
        setStaged(uploaded);
      }
      const result = await maskFormatsApi.preflightImport(projectId, {
        format_id: formatId,
        staged_object_key: uploaded.objectKey,
        staged_sha256: uploaded.digest,
        mapping: {
          labels: Object.fromEntries(Object.entries(mapping).filter(([, target]) => target.trim())),
        },
        options: {
          overwrite: false,
          ...(formatId === "youtube-vos" ? { sparse_gap_policy: sparseGapPolicy } : {}),
          ...(["coco-frames-seg", "mots"].includes(formatId) ? { frame_base: frameBase } : {}),
        },
      });
      setPreflight(result);
      setLossyConfirmed(false);
      setMapping((current) => ({
        ...Object.fromEntries(
          result.plan.unknown_labels.map((label) => [label, current[label] ?? ""]),
        ),
        ...current,
      }));
    } catch (error) {
      pushToast({
        msg: "格式预检失败",
        sub: error instanceof Error ? error.message : undefined,
        kind: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const execute = async () => {
    if (!preflight) return;
    setBusy(true);
    try {
      await maskFormatsApi.executeImport(
        projectId,
        preflight.receipt,
        preflight.plan.plan_digest,
        lossyConfirmed,
      );
      setQueued(true);
      pushToast({ msg: "标注导入已入队", sub: "可在任务铃查看进度", kind: "success" });
      onQueued?.();
    } catch (error) {
      pushToast({
        msg: "导入发起失败",
        sub: error instanceof Error ? error.message : undefined,
        kind: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      title="导入 Mask 标注"
      width={600}
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-[140px_1fr] items-center gap-3">
          <label htmlFor="mask-import-format" className="text-xs font-semibold text-foreground">
            格式
          </label>
          <select
            id="mask-import-format"
            value={formatId}
            onChange={(event) => {
              setFormatId(event.target.value);
              setFile(null);
              setStaged(null);
              resetPlan();
            }}
            className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
          >
            {availableFormats.map((format) => (
              <option key={format.format_id} value={format.format_id}>
                {format.label}
              </option>
            ))}
          </select>
          <label htmlFor="mask-import-file" className="text-xs font-semibold text-foreground">
            文件
          </label>
          <input
            id="mask-import-file"
            type="file"
            accept=".json,.zip,application/json,application/zip"
            onChange={(event) => {
              setFile(event.target.files?.[0] ?? null);
              setStaged(null);
              resetPlan();
            }}
            className="text-xs text-foreground file:mr-3 file:rounded-sm file:border file:border-border file:bg-card file:px-3 file:py-2 file:text-xs file:text-foreground"
          />
          {formatId === "youtube-vos" && (
            <>
              <label
                htmlFor="mask-import-gap-policy"
                className="text-xs font-semibold text-foreground"
              >
                稀疏帧策略
              </label>
              <select
                id="mask-import-gap-policy"
                value={sparseGapPolicy}
                onChange={(event) => {
                  setSparseGapPolicy(event.target.value as typeof sparseGapPolicy);
                  resetPlan();
                }}
                className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
              >
                <option value="outside_gaps">未标注帧记为 outside</option>
                <option value="nearest_hold">最近关键帧保持</option>
              </select>
            </>
          )}
          {["coco-frames-seg", "mots"].includes(formatId) && (
            <>
              <label
                htmlFor="mask-import-frame-base"
                className="text-xs font-semibold text-foreground"
              >
                帧号基准
              </label>
              <select
                id="mask-import-frame-base"
                value={frameBase}
                onChange={(event) => {
                  setFrameBase(Number(event.target.value) as 0 | 1);
                  resetPlan();
                }}
                className="rounded-sm border border-border bg-card px-3 py-2 text-xs text-foreground"
              >
                <option value={0}>0-based</option>
                <option value={1}>1-based</option>
              </select>
            </>
          )}
        </div>

        {preflight && (
          <div
            data-testid="mask-import-preflight"
            className={`flex flex-col gap-2 rounded-md border px-3 py-3 text-xs ${
              preflight.plan.loss_class === "unsupported"
                ? "border-status-danger/40 bg-status-danger/10"
                : preflight.plan.loss_class === "lossy"
                  ? "border-status-caution/40 bg-status-caution/10"
                  : "border-status-success/40 bg-status-success/10"
            }`}
          >
            <div className="flex justify-between gap-3 font-semibold text-foreground">
              <span>
                {preflight.plan.loss_class === "lossless"
                  ? "无损，可执行"
                  : preflight.plan.loss_class === "lossy"
                    ? "有损，需确认"
                    : "存在阻断项"}
              </span>
              <span>{preflight.plan.estimated_objects} 个实例</span>
            </div>
            {[...preflight.plan.losses, ...preflight.plan.skips, ...preflight.plan.warnings].map(
              (entry, index) => (
                <div key={`${entry.code}-${index}`} className="text-muted-foreground">
                  <CopyableError message={`${entry.code} · ${entry.message}`} />
                </div>
              ),
            )}
            {preflight.plan.unknown_labels.length > 0 && (
              <div className="flex flex-col gap-2 border-t border-border pt-2">
                <div className="font-semibold text-foreground">类别映射</div>
                {preflight.plan.unknown_labels.map((label) => (
                  <label key={label} className="grid grid-cols-[1fr_1fr] items-center gap-2">
                    <span className="truncate text-muted-foreground">{label}</span>
                    <input
                      value={mapping[label] ?? ""}
                      onChange={(event) =>
                        setMapping((current) => ({ ...current, [label]: event.target.value }))
                      }
                      placeholder="项目类别名"
                      className="rounded-sm border border-border bg-card px-2 py-1.5 text-foreground"
                    />
                  </label>
                ))}
                <Button size="sm" onClick={runPreflight} disabled={busy}>
                  应用映射并重新预检
                </Button>
              </div>
            )}
            {preflight.plan.loss_class === "lossy" && (
              <label className="flex items-center gap-2 text-foreground">
                <input
                  type="checkbox"
                  checked={lossyConfirmed}
                  onChange={(event) => setLossyConfirmed(event.target.checked)}
                />
                我已了解上述像素或结构损失
              </label>
            )}
          </div>
        )}

        {queued && (
          <div className="rounded-md border border-status-success/40 bg-status-success/10 px-3 py-2 text-xs text-foreground">
            任务已入队，可关闭窗口。
          </div>
        )}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            关闭
          </Button>
          {!queued && !preflight && (
            <Button onClick={runPreflight} disabled={busy || !file || !formatId}>
              {busy ? "上传并检查中…" : "上传并预检"}
            </Button>
          )}
          {!queued && preflight && preflight.plan.unknown_labels.length === 0 && (
            <Button
              onClick={execute}
              disabled={
                busy ||
                preflight.plan.loss_class === "unsupported" ||
                (preflight.plan.loss_class === "lossy" && !lossyConfirmed)
              }
            >
              {busy ? "提交中…" : "确认导入"}
            </Button>
          )}
        </div>
      </div>
    </Modal>
  );
}
