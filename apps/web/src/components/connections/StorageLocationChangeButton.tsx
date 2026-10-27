import { useEffect, useRef, useState } from "react";
import type {
  StorageLocationChangePreview,
  StorageLocationOut,
  StorageLocationUpdate,
} from "@/api/generated/types.gen";
import { ApiError, apiClient } from "@/api/client";
import { Button } from "@/components/ui/Button";
import { useToastStore } from "@/components/ui/Toast";
import { Modal } from "@/components/ui/Modal";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { cacheBytes, cachePolicyLabels } from "@/utils/cachePolicy";
import { StorageMaintenanceDetails } from "./StorageMaintenanceImpact";
import { storageAccessDenied } from "./storageQueryState";

interface Props {
  location: StorageLocationOut;
  payload: StorageLocationUpdate;
  label: string;
  disabled?: boolean;
  onApplied: () => Promise<void>;
  onDenied?: () => void;
}

const labels: Record<string, string> = {
  name: "位置名称",
  enabled: "启用状态",
  scope: "使用范围",
  use_user_ids: "指定人员授权",
  cache_policy: "缓存策略",
  original_cache_quota_bytes: "完整原图缓存额度",
  original_cache_ttl_seconds: "缓存保留时间",
};

function display(field: string, value: unknown): string {
  if (field === "enabled") return value ? "启用" : "停用";
  if (field === "scope") return value === "global" ? "所有数据管理员" : "仅指定的数据管理员";
  if (field === "use_user_ids") return `已授权 ${Array.isArray(value) ? value.length : 0} 人`;
  if (field === "original_cache_quota_bytes")
    return `${cacheBytes(Number(value))}（${Number(value).toLocaleString()} 字节）`;
  if (field === "original_cache_ttl_seconds") return `${value} 秒`;
  if (field === "cache_policy")
    return cachePolicyLabels[value as keyof typeof cachePolicyLabels] ?? String(value);
  return String(value ?? "未设置");
}

function changeError(error: unknown): string {
  const code =
    error instanceof ApiError && typeof error.detailRaw === "object" && error.detailRaw
      ? String((error.detailRaw as { code?: unknown }).code ?? "")
      : "";
  if (code === "storage_change_preview_expired") return "预览已过期，请重新预览。";
  if (["storage_change_preview_changed", "storage_location_revision_changed"].includes(code))
    return "候选配置或影响关系已变化，请刷新位置并重新预览。";
  if (code === "storage_change_preview_invalid") return "确认凭证无效，请重新预览。";
  return verifiedContentError(error);
}

export function StorageLocationChangeButton(props: Props) {
  const session = useSessionRevision();
  return (
    <ChangeOwner
      key={`${session}/${props.location.id}/${props.location.config_revision}/${JSON.stringify(props.payload)}`}
      {...props}
    />
  );
}

function ChangeOwner({ location, payload, label, disabled, onApplied, onDenied }: Props) {
  const capture = useSessionGuard();
  const pushToast = useToastStore((state) => state.push);
  const trigger = useRef<HTMLButtonElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const [preview, setPreview] = useState<StorageLocationChangePreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [expired, setExpired] = useState(false);
  const [stale, setStale] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState("");
  const sequence = useRef(0);
  useEffect(() => {
    if (!preview) return;
    const remaining = Date.parse(preview.expires_at) - Date.now();
    const timer = window.setTimeout(() => setExpired(true), Math.max(0, remaining));
    return () => window.clearTimeout(timer);
  }, [preview]);
  const close = () => {
    if (applying) return;
    sequence.current += 1;
    setPreview(null);
    setError("");
    setBusy(false);
  };
  const fail = (failure: unknown) => {
    setError(changeError(failure));
    if (storageAccessDenied(failure)) {
      setPreview(null);
      setDenied(true);
      onDenied?.();
    }
  };
  const load = async () => {
    const current = capture();
    const ticket = ++sequence.current;
    const owns = () => current() && sequence.current === ticket;
    setBusy(true);
    setStale(true);
    setError("");
    try {
      const result = await apiClient.post<StorageLocationChangePreview>(
        `/storage-locations/${location.id}/change-preview`,
        payload,
      );
      if (!owns()) return;
      if (
        result.location_id !== location.id ||
        result.config_revision !== payload.expected_revision ||
        result.impact.location_id !== location.id ||
        result.impact.config_revision !== location.config_revision ||
        !Number.isFinite(Date.parse(result.expires_at))
      ) {
        throw new Error("位置配置已变化，请刷新位置后重新预览。");
      }
      setPreview(result);
      setExpired(Date.parse(result.expires_at) <= Date.now());
      setStale(false);
    } catch (failure) {
      if (owns()) fail(failure);
    } finally {
      if (owns()) setBusy(false);
    }
  };
  const apply = async () => {
    if (!preview || busy || stale || expired || Date.parse(preview.expires_at) <= Date.now()) {
      setExpired(true);
      return;
    }
    const current = capture();
    setBusy(true);
    setApplying(true);
    setError("");
    let saved = false;
    try {
      await apiClient.patch(`/storage-locations/${location.id}`, {
        ...payload,
        impact_token: preview.impact_token,
      });
      saved = true;
      if (!current()) return;
      setPreview(null);
      pushToast({ msg: "位置配置已保存", kind: "success" });
      await onApplied();
    } catch (failure) {
      if (current()) {
        if (saved) setError(`位置配置已保存，但刷新失败。${changeError(failure)}`);
        else {
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
  const counts = preview?.impact.counts;
  return (
    <div className="min-w-0">
      <Button
        ref={trigger}
        size="sm"
        disabled={disabled || busy || denied}
        onClick={() => void load()}
      >
        {busy && !preview ? "正在预览…" : label}
      </Button>
      {!preview && error && (
        <p role="alert" className="mt-2 text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      <Modal
        open={!!preview}
        onClose={close}
        title="确认位置配置变更"
        width={640}
        stopEscapePropagation
        returnFocusRef={trigger}
        initialFocusRef={cancel}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancel} size="sm" disabled={applying} onClick={close}>
              返回修改
            </Button>
            <Button size="sm" disabled={busy} onClick={() => void load()}>
              重新预览
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={busy || expired || stale}
              onClick={() => void apply()}
            >
              {applying ? "正在提交…" : "确认保存位置配置"}
            </Button>
          </div>
        }
      >
        {preview && counts && (
          <div className="space-y-4 text-sm">
            <div>
              <p className="font-medium break-words">{location.name}</p>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                位置修订 {preview.config_revision} → {preview.config_revision + 1}
                。确认时会重新盘点已保存的关系。
              </p>
            </div>
            {preview.changes.length ? (
              <dl className="divide-y divide-border rounded-md border border-border px-3">
                {preview.changes.map((change) => (
                  <div
                    key={change.field}
                    className="grid gap-1 py-3 sm:grid-cols-[8rem_1fr] sm:gap-3"
                  >
                    <dt className="text-xs text-muted-foreground">
                      {labels[change.field] ?? change.field}
                    </dt>
                    <dd className="min-w-0 break-words">
                      <span className="text-muted-foreground">
                        {display(change.field, change.before)}
                      </span>
                      <span aria-label="变更为" className="mx-2 text-muted-foreground">
                        →
                      </span>
                      <span>{display(change.field, change.after)}</span>
                      {change.field === "use_user_ids" &&
                        Array.isArray(change.before) &&
                        Array.isArray(change.after) && (
                          <p className="mt-1 text-xs text-muted-foreground">
                            新增{" "}
                            {
                              change.after.filter(
                                (id) => !(change.before as unknown[]).includes(id),
                              ).length
                            }{" "}
                            人，撤回{" "}
                            {
                              change.before.filter(
                                (id) => !(change.after as unknown[]).includes(id),
                              ).length
                            }{" "}
                            人。人员选择以刚才的授权表单为准。
                          </p>
                        )}
                    </dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p className="text-xs text-muted-foreground">字段值未变化；保存仍会更新位置修订。</p>
            )}
            <div className="rounded-md border border-border bg-muted p-3 text-xs leading-relaxed">
              <p>
                更新位置修订会使旧完整原图缓存失配，后续回收仍受读租约保护。旧扫描与登记在后续检查点停止写入，自动同步在后续检查中阻塞。
              </p>
              {payload.enabled === false && (
                <p className="mt-2">
                  停用后拒绝新的引用原图读取，包括完整缓存；已就绪冻结内容仍按项目权限保留。
                </p>
              )}
              {(payload.scope != null || payload.use_user_ids != null) && (
                <p className="mt-2">撤回目录使用权影响来源管理；已有任务仍按项目权限读取。</p>
              )}
            </div>
            <p className="text-xs leading-relaxed text-muted-foreground">
              已保存关系：{counts.sources} 个来源 · {counts.datasets} 个数据集 · {counts.projects}{" "}
              个项目 · {counts.tasks} 个任务 · {counts.annotations} 条标注。
            </p>
            <details className="rounded-md border border-border">
              <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
                查看完整影响盘点与统计范围
              </summary>
              <div className="border-t border-border p-3">
                <StorageMaintenanceDetails data={preview.impact} />
              </div>
            </details>
            <p className="text-xs leading-relaxed text-muted-foreground">
              有效至 {new Date(preview.expires_at).toLocaleString("zh-CN")}
              。修改候选、配置或已盘点关系后需重新预览。
            </p>
            {(expired || stale) && (
              <p role="status" className="text-xs text-status-warning">
                {expired ? "预览已过期，请重新预览。" : "确认已失效，请重新预览后再保存。"}
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
