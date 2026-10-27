import { useEffect, useRef, useState } from "react";
import { datasetsApi, type DatasetLifecyclePreview, type DatasetResponse } from "@/api/datasets";
import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";

interface Props {
  dataset: DatasetResponse;
  onApplied: () => void | Promise<void>;
}
function lifecycleError(error: unknown) {
  const code =
    error instanceof ApiError && typeof error.detailRaw === "object" && error.detailRaw
      ? String((error.detailRaw as { code?: unknown }).code ?? "")
      : "";
  if (code === "dataset_lifecycle_preview_expired") return "预览已过期，请重新预览。";
  if (code === "dataset_lifecycle_preview_invalid") return "确认凭证无效，请重新预览。";
  if (["dataset_lifecycle_preview_changed", "dataset_lifecycle_changed"].includes(code))
    return "数据集状态或影响关系已变化，请刷新详情后重新预览。";
  if (code === "dataset_busy") return "数据集正在修改，请稍后重新预览。";
  return error instanceof Error ? error.message : "操作失败，请重新核对。";
}
function accessDenied(error: unknown) {
  return (
    !!error &&
    typeof error === "object" &&
    "status" in error &&
    [401, 403, 404].includes(Number(error.status))
  );
}

export function DatasetLifecycleAction(props: Props) {
  const session = useSessionRevision();
  return (
    <LifecycleOwner
      key={`${session}/${props.dataset.id}/${props.dataset.lifecycle_revision ?? 1}/${props.dataset.archived_at ?? "active"}`}
      {...props}
    />
  );
}
function LifecycleOwner({ dataset, onApplied }: Props) {
  const operation = dataset.archived_at ? "restore" : "archive";
  const action = operation === "archive" ? "归档" : "恢复";
  const payload = { operation, expected_revision: dataset.lifecycle_revision ?? 1 } as const;
  const capture = useSessionGuard();
  const toast = useToastStore((state) => state.push);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const sequence = useRef(0);
  const [review, setReview] = useState<DatasetLifecyclePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [expired, setExpired] = useState(false);
  const [stale, setStale] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!review) return;
    const timer = window.setTimeout(
      () => setExpired(true),
      Math.max(0, Date.parse(review.expires_at) - Date.now()),
    );
    return () => window.clearTimeout(timer);
  }, [review]);
  const close = () => {
    if (applying) return;
    sequence.current += 1;
    setReview(null);
    setError("");
    setBusy(false);
  };
  const fail = (failure: unknown) => {
    setError(lifecycleError(failure));
    if (accessDenied(failure)) {
      setDenied(true);
      setReview(null);
    }
  };
  const preview = async () => {
    const current = capture();
    const ticket = ++sequence.current;
    const owns = () => current() && sequence.current === ticket;
    setBusy(true);
    setStale(true);
    setError("");
    try {
      const result = await datasetsApi.previewLifecycle(dataset.id, payload);
      if (!owns()) return;
      if (
        result.dataset_id !== dataset.id ||
        result.lifecycle_revision !== payload.expected_revision ||
        result.operation !== operation ||
        result.state !== (dataset.archived_at ? "archived" : "active") ||
        !Number.isFinite(Date.parse(result.expires_at))
      ) {
        throw new Error("数据集状态已变化，请刷新详情后重新预览。");
      }
      setReview(result);
      setStale(false);
      setExpired(Date.parse(result.expires_at) <= Date.now());
    } catch (failure) {
      if (owns()) fail(failure);
    } finally {
      if (owns()) setBusy(false);
    }
  };
  const apply = async () => {
    if (!review || busy || stale || expired || Date.parse(review.expires_at) <= Date.now()) {
      setExpired(true);
      return;
    }
    const current = capture();
    setBusy(true);
    setApplying(true);
    setError("");
    let saved = false;
    try {
      const result = await datasetsApi.setLifecycle(dataset.id, {
        ...payload,
        impact_token: review.impact_token,
      });
      saved = true;
      if (!current()) return;
      if (
        result.dataset_id !== dataset.id ||
        result.state !== (operation === "archive" ? "archived" : "active") ||
        result.lifecycle_revision !== payload.expected_revision + 1
      )
        throw new Error("操作已提交，返回状态需要刷新核对。");
      setReview(null);
      toast({ msg: `数据集已${action}`, kind: "success" });
      await onApplied();
    } catch (failure) {
      if (current()) {
        if (saved) {
          setReview(null);
          setError(`操作已提交，但刷新失败。${lifecycleError(failure)}`);
        } else {
          setStale(true);
          fail(failure);
        }
      }
    } finally {
      if (current()) {
        setBusy(false);
        setApplying(false);
      }
    }
  };
  return (
    <div className="min-w-0">
      <Button ref={trigger} disabled={busy || denied} onClick={() => void preview()}>
        {busy && !review ? "正在预览…" : `${action}数据集`}
      </Button>
      {!review && error && (
        <p role="alert" className="mt-2 text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      <Modal
        open={!!review}
        onClose={close}
        title={`${action}数据集前确认`}
        width={600}
        stopEscapePropagation
        returnFocusRef={trigger}
        initialFocusRef={cancel}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancel} size="sm" disabled={applying} onClick={close}>
              取消
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void preview()}>
              重新预览
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={busy || stale || expired}
              onClick={() => void apply()}
            >
              {applying ? "正在提交…" : `确认${action}数据集`}
            </Button>
          </div>
        }
      >
        {review && (
          <div className="space-y-4 text-sm">
            <div>
              <p className="font-medium break-words">{dataset.name}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                生命周期修订 {review.lifecycle_revision} → {review.lifecycle_revision + 1}
              </p>
            </div>
            <div className="rounded-md border border-border bg-muted p-3 text-xs leading-relaxed">
              {operation === "archive" ? (
                <p>
                  归档后停止新接入、数据集修改、项目关联和新训练冻结。保留样本、来源历史、已有任务、标注和冻结内容；已有任务继续按当前项目权限使用。不会删除或改写源文件。
                </p>
              ) : (
                <p>
                  恢复只解除数据集归档，不恢复已撤销的人员权限、来源或同步委托，不自动启用来源、同步计划或重启归档前的后台任务。
                </p>
              )}
            </div>
            <dl className="divide-y divide-border">
              {[
                ["保留的内容", `${review.counts.items} 个样本 · ${review.counts.sources} 个来源`],
                [
                  "项目与工作",
                  `${review.counts.projects} 个项目 · ${review.counts.tasks} 个任务 · ${review.counts.annotations} 条标注`,
                ],
                [
                  "活动接入",
                  `${review.counts.active_scans} 个扫描 · ${review.counts.active_registrations} 个预检/登记 · ${review.counts.pending_jobs} 个未结束任务`,
                ],
                ["已启用周期计划", `${review.counts.enabled_schedules} 个`],
                [
                  "有成员证据的冻结版本",
                  `就绪 ${review.counts.ready_lineage_versions} · 未完成 ${review.counts.unfinished_lineage_versions}`,
                ],
              ].map(([label, value]) => (
                <div key={label} className="grid gap-1 py-2 sm:grid-cols-2 sm:gap-3">
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="min-w-0 break-words tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="text-xs leading-relaxed text-muted-foreground">
              冻结数量仅包含已记录数据集成员证据的版本，缺少证据的历史未计入。此操作不清理物理对象；已有冻结打包和内容仍按原权限保护。
            </p>
            <p className="text-xs leading-relaxed text-muted-foreground">
              统计于 {new Date(review.measured_at).toLocaleString("zh-CN")}，有效至{" "}
              {new Date(review.expires_at).toLocaleString("zh-CN")}
              。确认时重新检查权限、状态与关系。
            </p>
            {(expired || stale) && (
              <p role="status" className="text-xs text-status-warning">
                {expired ? "预览已过期，请重新预览。" : "确认已失效，请重新预览后再提交。"}
              </p>
            )}
            {error && (
              <p role="alert" className="text-xs leading-relaxed text-status-danger">
                {error}
              </p>
            )}
          </div>
        )}
      </Modal>
    </div>
  );
}
