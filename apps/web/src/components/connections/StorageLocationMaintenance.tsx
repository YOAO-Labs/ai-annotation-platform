import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import type {
  StorageLocationOut,
  StorageMaintenancePreview,
  StorageMaintenanceStatus,
} from "@/api/generated/types.gen";
import { ApiError } from "@/api/client";
import { type StorageMaintenanceOperation, storageMaintenanceApi } from "@/api/storageMaintenance";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { StorageMaintenanceDetails } from "./StorageMaintenanceImpact";
import { storageAccessDenied } from "./storageQueryState";

const labels = { active: "正常访问", draining: "正在等待访问收尾", stopped: "受控访问已停止" };
const actions = { drain: "开始维护", stop: "停止访问", resume: "恢复访问" };
const targets = { drain: "draining", stop: "stopped", resume: "active" } as const;
const operationLabels: Record<string, string> = {
  browse: "目录浏览",
  probe: "位置探测",
  scan: "目录扫描",
  scan_content: "扫描内容核验",
  registration_directory: "来源目录核验",
  reference_registration: "引用登记",
  reference_read: "引用原图读取",
  original_cache_read: "完整缓存读取",
  quality_read: "质量检查读取",
  connection_test: "已保存连接测试",
  connection_import: "连接器复制导入",
  connection_listing: "导入文件列举",
};
const scope =
  "仅涵盖平台对本逻辑位置的受控访问。其他位置可能共用目录或后端；此状态不能证明整块挂载、服务器或桶已静止，不提供卸载或物理删除许可。";

function maintenanceError(error: unknown) {
  const code =
    error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object"
      ? String((error.detailRaw as { code?: unknown }).code ?? "")
      : "";
  if (code === "storage_maintenance_preview_expired") return "预览已过期，请重新预览。";
  if (code === "storage_maintenance_preview_invalid") return "确认凭证无效，请重新预览。";
  if (["storage_maintenance_preview_changed", "storage_maintenance_changed"].includes(code))
    return "维护状态或影响关系已变化，请刷新状态后重新预览。";
  if (code === "storage_maintenance_busy") return "位置正在被使用或修改，请稍后刷新状态。";
  if (code === "storage_maintenance_unsettled")
    return "仍有未收尾访问，暂不能确认停止。请等待实际访问结束并刷新状态。";
  if (code === "storage_maintenance_protocol_unqualified")
    return "部署尚未启用维护控制，请由运维核实所有 API 和 Worker 已切换访问协议。";
  return verifiedContentError(error);
}

function validStatus(data: StorageMaintenanceStatus, locationId: string) {
  return (
    data.location_id === locationId &&
    ["active", "draining", "stopped"].includes(data.state) &&
    Number.isSafeInteger(data.revision) &&
    data.revision >= 1 &&
    Number.isSafeInteger(data.open_operations) &&
    data.open_operations >= 0 &&
    Array.isArray(data.operations) &&
    typeof data.protocol_enabled === "boolean" &&
    data.coverage === "registered_location_access_only" &&
    data.physical_cleanup_permission === false
  );
}

export function StorageLocationMaintenance({ location }: { location: StorageLocationOut }) {
  const session = useSessionRevision();
  const { role } = usePermissions();
  if (role !== "super_admin" || (location.kind !== "filesystem" && !location.connection_id))
    return null;
  return (
    <MaintenanceOwner
      key={`${session}/${location.id}/${location.config_revision}`}
      location={location}
    />
  );
}

function MaintenanceOwner({ location }: { location: StorageLocationOut }) {
  const client = useQueryClient();
  const session = useAuthStore((state) => state.sessionId);
  const revision = useSessionRevision();
  const [open, setOpen] = useState(false);
  const [actionDenied, setActionDenied] = useState(false);
  const key = ["storage-maintenance", session, revision, location.id, location.config_revision];
  const query = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => storageMaintenanceApi.status(location.id, signal),
    enabled: open,
    staleTime: 0,
    retry: false,
    refetchOnWindowFocus: false,
  });
  const denied = actionDenied || storageAccessDenied(query.error);
  const valid = query.data && validStatus(query.data, location.id);
  const data = denied || !valid ? undefined : query.data;
  const applied = async (result: StorageMaintenanceStatus) => {
    client.setQueryData(key, result);
    await Promise.all([
      client.invalidateQueries({ queryKey: ["storage-location"] }),
      client.invalidateQueries({ queryKey: ["storage-source-locations"] }),
      client.invalidateQueries({ queryKey: ["storage-maintenance"] }),
    ]);
  };
  return (
    <details
      className="mt-4 rounded-lg border border-border text-sm"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-4 py-3 font-medium">位置访问维护</summary>
      {open && (
        <div className="min-w-0 space-y-3 border-t border-border p-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs leading-relaxed text-muted-foreground">
              关闭新访问后，等待已登记的实际 I/O 收尾，再确认停止。
            </p>
            <Button size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
              刷新维护状态
            </Button>
          </div>
          {query.isPending && (
            <p role="status" className="text-xs text-muted-foreground">
              正在读取维护状态…
            </p>
          )}
          {(query.error || actionDenied || (query.data && !valid)) && (
            <p role="alert" className="text-xs leading-relaxed text-status-danger">
              {data ? "刷新失败，以下为上次状态；操作已暂停。" : "无法读取维护状态。"}
              {query.error
                ? maintenanceError(query.error)
                : actionDenied
                  ? "访问权限已变化，请重新打开详情。"
                  : "位置或状态不匹配，请重新打开详情。"}
            </p>
          )}
          {data && (
            <>
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <span className="font-medium">{labels[data.state]}</span>
                <span className="text-xs text-muted-foreground">
                  维护修订 {data.revision} · 未收尾访问 {data.open_operations}
                </span>
              </div>
              <p className="text-xs text-muted-foreground">
                统计于 {new Date(data.measured_at).toLocaleString("zh-CN")}
              </p>
              {!data.protocol_enabled && (
                <p className="text-xs leading-relaxed text-muted-foreground">
                  部署尚未启用维护控制。运维须先核实所有 API 和 Worker
                  已切换访问协议，空账本不能代替部署核验。
                </p>
              )}
              {data.state !== "active" && (
                <p className="text-xs leading-relaxed text-muted-foreground">
                  新访问已被拒绝。已有独立冻结内容继续按原权限读取；维护不会自动取消远端推理或撤回已经下载的字节。
                </p>
              )}
              {data.open_operations > 0 && (
                <div className="space-y-2 text-xs leading-relaxed">
                  <p className="text-status-warning">
                    未收尾记录可能仍在执行或状态未确认。超时、作业终态和心跳失联都不能证明线程已结束。
                  </p>
                  <ul className="divide-y divide-border">
                    {data.operations.map((operation) => (
                      <li key={operation.id} className="py-2">
                        <p className="break-words">
                          {operationLabels[operation.purpose] ?? "受控存储访问"} ·{" "}
                          {operation.node_id}
                        </p>
                        <p className="mt-1 text-muted-foreground">
                          修订 {operation.revision} · 开始于{" "}
                          {new Date(operation.started_at).toLocaleString("zh-CN")}
                        </p>
                      </li>
                    ))}
                  </ul>
                  {data.has_more && (
                    <p className="text-muted-foreground">
                      仅显示前 50 条，未收尾总数仍按全部记录计算。
                    </p>
                  )}
                </div>
              )}
              <MaintenanceActions
                key={`${data.state}/${data.revision}`}
                location={location}
                status={data}
                disabled={!!query.error || query.isFetching || !data.protocol_enabled}
                onApplied={applied}
                onDenied={() => setActionDenied(true)}
              />
            </>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground">{scope}</p>
        </div>
      )}
    </details>
  );
}

function MaintenanceActions({
  location,
  status,
  disabled,
  onApplied,
  onDenied,
}: {
  location: StorageLocationOut;
  status: StorageMaintenanceStatus;
  disabled: boolean;
  onApplied: (result: StorageMaintenanceStatus) => Promise<void>;
  onDenied: () => void;
}) {
  const capture = useSessionGuard();
  const toast = useToastStore((state) => state.push);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const sequence = useRef(0);
  const [review, setReview] = useState<StorageMaintenancePreview | null>(null);
  const [operation, setOperation] = useState<StorageMaintenanceOperation>("drain");
  const [busy, setBusy] = useState(false);
  const [applying, setApplying] = useState(false);
  const [expired, setExpired] = useState(false);
  const [stale, setStale] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!review) return;
    const timeout = window.setTimeout(
      () => setExpired(true),
      Math.max(0, Date.parse(review.expires_at) - Date.now()),
    );
    return () => window.clearTimeout(timeout);
  }, [review]);
  const close = () => {
    if (applying) return;
    sequence.current += 1;
    setReview(null);
    setBusy(false);
    setError("");
  };
  const failure = (error: unknown) => {
    setError(maintenanceError(error));
    if (storageAccessDenied(error)) {
      setDenied(true);
      setReview(null);
      onDenied();
    }
  };
  const preview = async (next: StorageMaintenanceOperation) => {
    if (disabled || denied || busy) return;
    const current = capture();
    const ticket = ++sequence.current;
    const owns = () => current() && sequence.current === ticket;
    setOperation(next);
    setBusy(true);
    setStale(true);
    setError("");
    try {
      const result = await storageMaintenanceApi.preview(location.id, next, status.revision);
      if (!owns()) return;
      if (
        !validStatus(result, location.id) ||
        result.revision !== status.revision ||
        result.state !== status.state ||
        result.operation !== next ||
        result.target_state !== targets[next] ||
        result.impact.location_id !== location.id ||
        result.impact.config_revision !== location.config_revision ||
        !Number.isFinite(Date.parse(result.expires_at))
      )
        throw new Error("维护状态或位置配置已变化，请刷新状态后重新预览。");
      setReview(result);
      setExpired(Date.parse(result.expires_at) <= Date.now());
      setStale(false);
    } catch (error) {
      if (owns()) failure(error);
    } finally {
      if (owns()) setBusy(false);
    }
  };
  const apply = async () => {
    if (disabled || denied || applying) return;
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
      const result = await storageMaintenanceApi.apply(location.id, review);
      saved = true;
      if (!current()) return;
      if (
        !validStatus(result, location.id) ||
        result.state !== review.target_state ||
        result.revision !== review.revision + 1
      )
        throw new Error("返回状态需要刷新核对。");
      setReview(null);
      toast({
        msg: `位置已${operation === "drain" ? "关闭新访问" : operation === "stop" ? "停止受控访问" : "恢复访问"}`,
        kind: "success",
      });
      await onApplied(result);
    } catch (error) {
      if (current()) {
        if (saved) {
          setReview(null);
          setError(`操作已提交，但刷新失败。${maintenanceError(error)}`);
        } else {
          setStale(true);
          failure(error);
        }
      }
    } finally {
      if (current()) {
        setBusy(false);
        setApplying(false);
      }
    }
  };
  const available: StorageMaintenanceOperation[] =
    status.state === "active"
      ? ["drain"]
      : status.state === "draining"
        ? ["stop", "resume"]
        : ["resume"];
  return (
    <div>
      <div className="flex flex-wrap gap-2">
        {available.map((item) => (
          <Button
            key={item}
            size="sm"
            disabled={disabled || busy || denied || (item === "stop" && status.open_operations > 0)}
            onClick={(event) => {
              trigger.current = event.currentTarget;
              void preview(item);
            }}
          >
            {actions[item]}
          </Button>
        ))}
      </div>
      {error && !review && (
        <p role="alert" className="mt-2 text-xs leading-relaxed text-status-danger">
          {error}
        </p>
      )}
      <Modal
        open={!!review}
        onClose={close}
        title={`${actions[operation]}前确认`}
        width={600}
        stopEscapePropagation
        returnFocusRef={trigger}
        initialFocusRef={cancel}
        footer={
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancel} size="sm" disabled={applying} onClick={close}>
              取消
            </Button>
            <Button size="sm" disabled={disabled || busy} onClick={() => void preview(operation)}>
              重新预览
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={disabled || busy || stale || expired}
              onClick={() => void apply()}
            >
              {applying ? "正在提交…" : `确认${actions[operation]}`}
            </Button>
          </div>
        }
      >
        {review && (
          <div className="space-y-4 text-sm">
            <div>
              <p className="font-medium break-words">{location.name}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                {labels[review.state]} → {labels[review.target_state]} · 维护修订 {review.revision}{" "}
                → {review.revision + 1}
              </p>
            </div>
            <p className="rounded-md border border-border bg-muted p-3 text-xs leading-relaxed">
              {operation === "drain"
                ? "关闭本位置的新访问。已登记访问必须实际结束；等待期间不会自动停止所有业务作业，不能据此卸载存储。"
                : operation === "stop"
                  ? "提交时再次确认未收尾访问为零，并记录受控访问已停止。其他位置和外部客户端不在此范围内。"
                  : "恢复新访问准入，并使用新的维护代次。不会重放已失效的作业、权限或旧提交证明。"}
            </p>
            <p>
              本次预览未收尾访问：<span className="tabular-nums">{review.open_operations}</span>
            </p>
            <details className="rounded-md border border-border">
              <summary className="cursor-pointer p-3 text-xs font-medium">
                查看已保存关系的影响盘点
              </summary>
              <div className="border-t border-border p-3">
                <StorageMaintenanceDetails data={review.impact} />
              </div>
            </details>
            <p className="text-xs leading-relaxed text-muted-foreground">{scope}</p>
            {error && (
              <p role="alert" className="text-xs leading-relaxed text-status-danger">
                {error}
              </p>
            )}
            {(expired || stale) && (
              <p role="status" className="text-xs text-status-warning">
                {expired
                  ? "预览已过期，请重新预览。"
                  : "请核对变化并重新预览，旧凭证不能继续提交。"}
              </p>
            )}
            <p className="text-xs leading-relaxed text-muted-foreground">
              统计于 {new Date(review.measured_at).toLocaleString("zh-CN")}，有效至{" "}
              {new Date(review.expires_at).toLocaleString("zh-CN")}
              。提交时重新核验权限、代次和关系。
            </p>
          </div>
        )}
      </Modal>
    </div>
  );
}
