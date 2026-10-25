import { useEffect, useRef, useState, type MutableRefObject, type RefObject } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import { storageMigrationCopiesApi, type MigrationCopyItem } from "@/api/storageMigrationCopies";
import {
  storageMigrationWindowsApi,
  type MigrationWindowFinalization,
  type MigrationWindowItem,
} from "@/api/storageMigrationWindows";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { copyAccessDenied } from "./MigrationCopyPrepareModal";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const WINDOW_STATES = {
  draining: "等待访问收尾",
  stopped: "已停止登记访问",
  closing: "关闭中",
  closed: "已关闭",
};
const BOUNDARY =
  "登记访问已停止不等于全源停写，也不代表迁移可切换。旧上传链接、未登记调用及存储端自动过期仍需另行核实。";
const FINALIZATION_STATES = {
  bound: "已绑定，待最终核验",
  sealing: "正在最终核验",
  sealed: "最终核验已封存",
  invalidating: "等待收尾并解除绑定",
  invalidated: "绑定已解除",
};
const FINALIZATION_HINTS = {
  bound: "运维已绑定此窗口，尚未封存最终清单与复制证据。",
  sealing: "正在处理此窗口的最终清单与准确版本复制，请等待任务收尾。",
  sealed: "此窗口的最终清单与准确版本复制证据已封存。",
  invalidating: "等待相关任务和物理操作收尾，再由运维显式解除绑定；普通访问不会自动恢复。",
  invalidated: "最终核验绑定已解除；访问是否恢复，仍以窗口状态为准。",
};
function stamp(value: string) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN") : "时间未知";
}
function pageIndex(value: string | null) {
  const page = Number(value ?? 1);
  return Number.isSafeInteger(page) && page > 0 && page <= 1000000 ? page - 1 : 0;
}
export function migrationWindowError(error: unknown) {
  const messages: Record<string, string> = {
    migration_source_read_denied:
      "源读取未获准，请核实迁移窗口状态与源访问门禁；关闭的窗口不能继续读取。",
    copy_window_changed: "复制批次与窗口绑定已变化，请查看绑定窗口并核实当前状态。",
    copy_window_requires_generation: "窗口清单须沿固定父复制批次准备后续批次，不能建立初次复制。",
    migration_window_exists: "源存储已有开放窗口，请刷新窗口历史。",
    migration_window_changed: "窗口状态已变化，已重新读取；请审阅当前状态后重试。",
    migration_window_state_invalid: "当前窗口状态不支持此操作，请刷新后重试。",
    migration_window_unsettled: "仍有注册操作尚未收尾，不能确认停止。请等待或核实 Worker 状态。",
    migration_window_source_changed: "源存储配置已变化，不能继续使用此窗口。",
    migration_window_gate_changed: "源访问门禁已变化，请核实当前窗口。",
    finalization_active: "窗口仍绑定最终核验，请由运维在任务收尾后显式解除绑定，再关闭窗口。",
    finalization_changed: "最终核验绑定已变化，已重新读取；请审阅当前状态后重试。",
    finalization_unsettled: "最终核验仍有任务或物理操作尚未收尾，请等待或由运维核实。",
    finalization_closing: "最终核验正在等待收尾并解除绑定，不能继续读取来源。",
    finalization_copy_read_only: "此最终复制批次已封存，只能查看，不能继续执行。",
    storage_maintenance_protocol_unqualified: "当前部署尚未具备完整维护协议，无法开启窗口。",
    copy_not_prepared: "所选复制批次尚未封存计划。",
    copy_active_conflict: "所选复制批次仍有活动任务，请等待结束。",
    copy_execution_unsettled: "所选批次的底层操作尚未确认收尾，请等待或核实 Worker 状态。",
    copy_read_only: "所选批次已有后续批次，请选择当前可继续的批次。",
  };
  if (
    error instanceof ApiError &&
    error.detailRaw &&
    typeof error.detailRaw === "object" &&
    "code" in error.detailRaw
  )
    return messages[String(error.detailRaw.code)] ?? verifiedContentError(error);
  return verifiedContentError(error);
}
export function MigrationWindowBinding({
  windowId,
  windowRevision,
  onOpen,
  buttonRef,
}: {
  windowId?: string | null;
  windowRevision?: number | null;
  onOpen: (button: HTMLButtonElement) => void;
  buttonRef?: RefObject<HTMLButtonElement>;
}) {
  if (!windowId)
    return <p className="text-xs text-muted-foreground">普通清单路径 · 未绑定迁移窗口</p>;
  return (
    <div className="space-y-1">
      <p className="break-all text-xs text-muted-foreground">
        迁移窗口：{windowId} · 绑定修订 {windowRevision ?? "未知"}
      </p>
      <Button ref={buttonRef} size="sm" onClick={(event) => onOpen(event.currentTarget)}>
        查看迁移窗口
      </Button>
    </div>
  );
}

export function MigrationWindowPanel({
  onDenied,
  returnFocusRef,
}: {
  onDenied: () => void;
  returnFocusRef: MutableRefObject<HTMLElement | null>;
}) {
  const revision = useSessionRevision();
  const [params, setParams] = useSearchParams();
  const [expanded, setExpanded] = useState(false);
  const [creating, setCreating] = useState(false);
  const page = pageIndex(params.get("migration_window_page"));
  const size = listPageSize(params.get("migration_window_page_size"));
  const rawId = params.get("migration_window") ?? "";
  const id = UUID.test(rawId) ? rawId : "";
  const open = expanded || !!id || params.has("migration_window_page");
  const patch = (change: Record<string, string | null>, replace = false) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        Object.entries(change).forEach(([key, value]) =>
          value === null ? next.delete(key) : next.set(key, value),
        );
        return next;
      },
      { replace },
    );
  const history = useQuery({
    queryKey: ["storage-migration-windows", revision, page, size],
    queryFn: ({ signal }) => storageMigrationWindowsApi.list(page * size, size, signal),
    enabled: open,
    retry: false,
    refetchInterval: (query) =>
      open &&
      !copyAccessDenied(query.state.error) &&
      query.state.data?.items.some((item) => item.state === "draining" || item.state === "closing")
        ? 5000
        : false,
  });
  const denied = copyAccessDenied(history.error);
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  useEffect(() => {
    if (!history.data || history.isFetching) return;
    const last = Math.max(0, Math.ceil(history.data.total / size) - 1);
    if (page > last)
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("migration_window_page", String(last + 1));
          return next;
        },
        { replace: true },
      );
  }, [history.data, history.isFetching, page, size, setParams]);
  return (
    <>
      <Card>
        <section aria-label="迁移窗口" className="p-4">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h2 className="text-md font-semibold">迁移维护窗口</h2>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                从已封存复制批次开启，停止普通登记访问后建立窗口清单。全源停写未证明。
              </p>
            </div>
            <Button
              disabled={denied}
              onClick={(event) => {
                returnFocusRef.current = event.currentTarget;
                setCreating(true);
              }}
            >
              开启迁移窗口
            </Button>
          </div>
          <details
            open={open}
            onToggle={(event) => setExpanded(event.currentTarget.open)}
            className="mt-3"
          >
            <summary className="cursor-pointer text-sm font-medium">窗口历史</summary>
            <div className="mt-3 space-y-3">
              <Button
                size="sm"
                disabled={history.isFetching || denied}
                onClick={() => void history.refetch()}
              >
                刷新窗口历史
              </Button>
              {history.isFetching && (
                <p role="status" className="text-xs text-muted-foreground">
                  正在读取迁移窗口…
                </p>
              )}
              {history.error && (
                <p role="alert" className="text-xs text-status-danger">
                  {denied ? "当前账号已无权读取迁移窗口。" : "窗口历史读取失败，可刷新重试。"}
                  {migrationWindowError(history.error)}
                </p>
              )}
              {!denied && history.data && (
                <>
                  {!history.data.items.length && (
                    <p className="text-sm text-muted-foreground">尚无迁移窗口。</p>
                  )}
                  <ul className="divide-y divide-border">
                    {history.data.items.map((item) => (
                      <li
                        key={item.window_id}
                        className="flex flex-wrap items-center justify-between gap-3 py-3 text-sm"
                      >
                        <div>
                          <p>
                            {stamp(item.created_at)} · {WINDOW_STATES[item.state]}
                          </p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            注册操作 {item.registered_operations} · 修订 {item.revision}
                          </p>
                        </div>
                        <Button
                          size="sm"
                          onClick={(event) => {
                            returnFocusRef.current = event.currentTarget;
                            patch({
                              migration_window: item.window_id,
                              migration_window_return: null,
                            });
                          }}
                        >
                          查看窗口详情
                        </Button>
                      </li>
                    ))}
                  </ul>
                  <ListPagination
                    label="迁移窗口"
                    page={page}
                    pageSize={size}
                    total={history.data.total}
                    loading={history.isFetching}
                    onPageChange={(value) => patch({ migration_window_page: String(value + 1) })}
                    onPageSizeChange={(value) =>
                      patch({
                        migration_window_page: "1",
                        migration_window_page_size: String(value),
                      })
                    }
                  />
                </>
              )}
            </div>
          </details>
        </section>
      </Card>
      {creating && !denied && (
        <CreateWindow
          onClose={() => setCreating(false)}
          onDenied={onDenied}
          returnFocusRef={returnFocusRef}
          onCreated={(item) => {
            setCreating(false);
            patch({ migration_window: item.window_id, migration_window_return: null });
          }}
        />
      )}
      {id && !denied && (
        <WindowDetail
          key={`${revision}/${id}`}
          id={id}
          onDenied={onDenied}
          returnFocusRef={returnFocusRef}
          onClose={() => patch({ migration_window: null })}
          onParent={(copyId) =>
            patch({
              migration_window: null,
              migration_job: null,
              migration_copy: copyId,
              migration_window_return: null,
            })
          }
        />
      )}
    </>
  );
}
function WindowDetail({
  id,
  onDenied,
  returnFocusRef,
  onClose,
  onParent,
}: {
  id: string;
  onDenied: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  onParent: (id: string) => void;
}) {
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const cancel = useRef<HTMLButtonElement>(null);
  const key = ["storage-migration-window", revision, id];
  const detail = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => storageMigrationWindowsApi.get(id, signal),
    retry: false,
    refetchInterval: (query) =>
      !copyAccessDenied(query.state.error) &&
      ["draining", "closing"].includes(query.state.data?.state ?? "")
        ? 5000
        : false,
  });
  const denied = copyAccessDenied(detail.error);
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  const item = denied || detail.isError ? undefined : detail.data;
  const finalizationBlocksClose = !!item?.finalization && item.finalization.state !== "invalidated";
  const transition = async (action: "stop" | "close") => {
    if (!item || busy || detail.isFetching || (action === "close" && finalizationBlocksClose))
      return;
    const current = capture();
    setBusy(true);
    setError("");
    try {
      const next = await storageMigrationWindowsApi.transition(id, action, item.revision);
      if (!current()) return;
      client.setQueryData(key, next);
      await Promise.all([
        client.invalidateQueries({ queryKey: ["storage-migration-windows"] }),
        client.invalidateQueries({ queryKey: ["storage-migration-window-options"] }),
      ]);
    } catch (failure) {
      if (!current()) return;
      setError(migrationWindowError(failure));
      if (copyAccessDenied(failure)) onDenied();
      else await client.invalidateQueries({ queryKey: key });
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <Modal
      open
      title="迁移窗口详情"
      width={700}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      initialFocusRef={cancel}
    >
      <div className="space-y-4 text-sm">
        {detail.isFetching && (
          <p role="status" className="text-xs text-muted-foreground">
            正在读取窗口状态…
          </p>
        )}
        {detail.error && (
          <p role="alert" className="text-xs text-status-danger">
            无法读取当前窗口状态。{migrationWindowError(detail.error)}
          </p>
        )}
        {error && (
          <p role="alert" className="text-xs text-status-danger">
            {error}
          </p>
        )}
        {item && (
          <>
            <section className="space-y-2 rounded-md border border-border p-3">
              <Badge variant="outline">{WINDOW_STATES[item.state]}</Badge>
              <p className="tabular-nums">注册操作：{item.registered_operations}</p>
              <p>
                {item.state === "closed"
                  ? "此窗口已结束登记访问排空检查。"
                  : item.registered_operations > 0
                    ? "仍有注册操作未收尾；不能确认停止，关闭也不会恢复普通访问。"
                    : item.registered_access_drained
                      ? "登记访问已排空。"
                      : "注册操作数为零，仍需显式确认停止。"}
              </p>
              {item.state === "closing" && (
                <p>窗口读取已禁止。操作全部收尾后，请重试关闭；本页不会自动恢复访问。</p>
              )}
              {item.state === "closed" && (
                <p>窗口已关闭，普通注册访问已恢复；此窗口不可继续读取。</p>
              )}
              <p className="text-xs leading-relaxed text-muted-foreground">{BOUNDARY}</p>
            </section>
            {item.finalization && (
              <FinalizationSummary finalization={item.finalization} busy={busy} onCopy={onParent} />
            )}
            <dl className="space-y-2 break-all text-xs text-muted-foreground">
              <div>
                <dt>窗口</dt>
                <dd>{item.window_id}</dd>
              </div>
              <div>
                <dt>父复制批次</dt>
                <dd>{item.parent_copy_id}</dd>
              </div>
              <div>
                <dt>目标位置</dt>
                <dd>{item.target_location_id}</dd>
              </div>
              <div>
                <dt>修订</dt>
                <dd>
                  窗口 {item.revision} · 源门禁 {item.source_gate_revision} · 源配置{" "}
                  {item.source_config_revision}
                </dd>
              </div>
              <div>
                <dt>创建 / 更新</dt>
                <dd>
                  {stamp(item.created_at)} / {stamp(item.updated_at)}
                </dd>
              </div>
            </dl>
            <div className="flex flex-wrap gap-2">
              {item.state === "draining" && (
                <Button
                  variant="primary"
                  disabled={busy || detail.isFetching || item.registered_operations !== 0}
                  onClick={() => void transition("stop")}
                >
                  确认停止登记访问
                </Button>
              )}
              {item.state !== "closed" && (
                <Button
                  disabled={busy || detail.isFetching || finalizationBlocksClose}
                  aria-describedby={
                    finalizationBlocksClose ? "migration-window-close-hint" : undefined
                  }
                  onClick={() => void transition("close")}
                >
                  {item.state === "closing" ? "重试关闭窗口" : "关闭窗口"}
                </Button>
              )}
              <Button disabled={busy} onClick={() => onParent(item.parent_copy_id)}>
                查看父复制批次
              </Button>
            </div>
            {finalizationBlocksClose && item.state !== "closed" && (
              <p
                id="migration-window-close-hint"
                className="text-xs leading-relaxed text-muted-foreground"
              >
                窗口仍绑定最终核验。请由运维在任务收尾后显式解除绑定，再关闭窗口。
              </p>
            )}
          </>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={busy || detail.isFetching}
            onClick={() => void detail.refetch()}
          >
            刷新窗口状态
          </Button>
          <Button ref={cancel} onClick={onClose}>
            返回原页面
          </Button>
        </div>
      </div>
    </Modal>
  );
}
function FinalizationSummary({
  finalization,
  busy,
  onCopy,
}: {
  finalization: MigrationWindowFinalization;
  busy: boolean;
  onCopy: (id: string) => void;
}) {
  const copyId = finalization.copy_id;
  const count = (key: string) => {
    const value = finalization.counts?.[key];
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
      ? value.toLocaleString("zh-CN")
      : "未知";
  };
  return (
    <section
      aria-labelledby="migration-window-finalization-title"
      className="space-y-2 rounded-md border border-border p-3"
    >
      <h3 id="migration-window-finalization-title" className="text-md font-medium">
        最终核验
      </h3>
      <Badge variant="outline">{FINALIZATION_STATES[finalization.state]}</Badge>
      <p>{FINALIZATION_HINTS[finalization.state]}</p>
      {finalization.counts ? (
        <p className="tabular-nums">
          记录中的当前对象：复制 {count("copied_objects")} · 保留 {count("retained_objects")} · 重建{" "}
          {count("rebuild_objects")}
        </p>
      ) : (
        <p className="text-xs text-muted-foreground">尚无当前对象处理计数。</p>
      )}
      <p className="text-xs text-muted-foreground">
        原生观测时间：
        {finalization.native_observed_at ? stamp(finalization.native_observed_at) : "暂无记录"}
        {finalization.sealed_at && ` · 封存时间：${stamp(finalization.sealed_at)}`}
      </p>
      <p className="text-xs leading-relaxed text-muted-foreground">
        原生观测是历史记录，不证明来源当前仍处于物理封闭状态。封存不代表业务切换或迁移完成。
      </p>
      {finalization.error_code && (
        <p role="alert" className="text-xs text-status-danger">
          最终核验记录了未完成情况，请由运维核实任务与本机来源状态。
        </p>
      )}
      {copyId && (
        <Button size="sm" disabled={busy} onClick={() => onCopy(copyId)}>
          查看最终复制批次
        </Button>
      )}
    </section>
  );
}
function eligible(item: MigrationCopyItem) {
  return (
    item.prepared &&
    !item.read_only &&
    !item.successor_copy_id &&
    !["pending", "running"].includes(item.status ?? "")
  );
}
function CreateWindow({
  onClose,
  onCreated,
  onDenied,
  returnFocusRef,
}: {
  onClose: () => void;
  onCreated: (item: MigrationWindowItem) => void;
  onDenied: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const client = useQueryClient();
  const cancel = useRef<HTMLButtonElement>(null);
  const [page, setPage] = useState(0);
  const [size, setSize] = useState(20);
  const [selected, setSelected] = useState<MigrationCopyItem | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const copies = useQuery({
    queryKey: ["storage-migration-window-parents", revision, page, size],
    queryFn: ({ signal }) => storageMigrationCopiesApi.list(page * size, size, signal),
    retry: false,
  });
  const denied = copyAccessDenied(copies.error);
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  useEffect(() => {
    if (!copies.data || copies.isFetching) return;
    const last = Math.max(0, Math.ceil(copies.data.total / size) - 1);
    if (page > last) setPage(last);
    if (selected) {
      const updated = copies.data.items.find((item) => item.copy_id === selected.copy_id);
      if (updated && updated !== selected) {
        setSelected(updated);
        if (!eligible(updated)) setConfirmed(false);
      }
    }
  }, [copies.data, copies.isFetching, page, size, selected]);
  const options = (denied ? [] : (copies.data?.items ?? [])).filter(eligible);
  const choices =
    selected && !options.some((item) => item.copy_id === selected.copy_id)
      ? [selected, ...options]
      : options;
  const submit = async () => {
    if (
      !selected ||
      !eligible(selected) ||
      !confirmed ||
      busy ||
      copies.isFetching ||
      copies.isError
    )
      return;
    const current = capture();
    setBusy(true);
    setError("");
    try {
      const item = await storageMigrationWindowsApi.create(selected.copy_id);
      if (!current()) return;
      void client.invalidateQueries({ queryKey: ["storage-migration-windows"] });
      onCreated(item);
    } catch (failure) {
      if (current()) {
        setError(migrationWindowError(failure));
        if (copyAccessDenied(failure)) onDenied();
      }
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <Modal
      open
      title="开启迁移窗口"
      width={700}
      onClose={onClose}
      returnFocusRef={returnFocusRef}
      initialFocusRef={cancel}
    >
      <div className="space-y-4 text-sm">
        <div>
          <label htmlFor="migration-window-parent" className="mb-1 block">
            父复制批次
          </label>
          <select
            id="migration-window-parent"
            value={selected?.copy_id ?? ""}
            disabled={busy || denied}
            aria-describedby="migration-window-parent-hint"
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
            onChange={(event) => {
              setSelected(choices.find((item) => item.copy_id === event.target.value) ?? null);
              setConfirmed(false);
            }}
          >
            <option value="" disabled hidden />
            {choices.map((item) => (
              <option key={item.copy_id} value={item.copy_id}>
                {item.target_name ?? "已登记目标"} · 第 {item.generation ?? 1} 批 ·{" "}
                {stamp(item.created_at)}
              </option>
            ))}
          </select>
          <p
            id="migration-window-parent-hint"
            className="mt-1 text-xs leading-relaxed text-muted-foreground"
          >
            仅列出已封存、无活动任务且没有后续批次的计划。底层操作是否真正收尾、源与目标身份是否仍匹配，由服务端复核。
          </p>
        </div>
        {copies.isFetching && (
          <p role="status" className="text-xs text-muted-foreground">
            正在读取父复制批次…
          </p>
        )}
        {copies.error && (
          <p role="alert" className="text-xs text-status-danger">
            无法读取父复制批次。{migrationWindowError(copies.error)}
          </p>
        )}
        {!copies.isFetching && !copies.isError && !options.length && (
          <p className="text-xs text-muted-foreground">
            本页无可选择批次，请先完成复制计划准备或查看其他页。
          </p>
        )}
        <Button
          size="sm"
          disabled={busy || copies.isFetching || denied}
          onClick={() => void copies.refetch()}
        >
          刷新父复制批次
        </Button>
        {copies.data && !denied && (
          <ListPagination
            label="窗口父复制批次"
            page={page}
            pageSize={size}
            total={copies.data.total}
            loading={copies.isFetching}
            onPageChange={setPage}
            onPageSizeChange={(value) => {
              setSize(value);
              setPage(0);
            }}
          />
        )}
        <section className="space-y-3 rounded-md border border-border p-3">
          <p>
            开启后，源存储的普通注册访问将停止接纳新操作，已有操作等待收尾，可能影响正在使用该源的数据集及后台任务。
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {BOUNDARY} 取消清单或复制任务不会关闭窗口；需显式关闭窗口才能恢复普通注册访问。
          </p>
          <label className="flex items-start gap-2">
            <input
              type="checkbox"
              className="mt-1"
              checked={confirmed}
              disabled={busy || denied || !selected || !eligible(selected)}
              onChange={(event) => setConfirmed(event.target.checked)}
            />
            <span>我确认停止源存储的普通注册访问，并知悉尚未证明完全物理静默。</span>
          </label>
        </section>
        {error && (
          <p role="alert" className="text-xs text-status-danger">
            {error}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            variant="primary"
            disabled={
              !selected ||
              !eligible(selected) ||
              !confirmed ||
              busy ||
              denied ||
              copies.isFetching ||
              copies.isError
            }
            onClick={() => void submit()}
          >
            {busy ? "开启中…" : "确认开启窗口"}
          </Button>
          <Button ref={cancel} onClick={onClose}>
            取消
          </Button>
        </div>
      </div>
    </Modal>
  );
}

export function MigrationWindowSelect({
  onSelection,
  onDenied,
  disabled,
}: {
  onSelection: (id: string, valid: boolean) => void;
  onDenied: () => void;
  disabled: boolean;
}) {
  const revision = useSessionRevision();
  const [enabled, setEnabled] = useState(false);
  const [page, setPage] = useState(0);
  const [size, setSize] = useState(20);
  const [selected, setSelected] = useState<MigrationWindowItem | null>(null);
  const windows = useQuery({
    queryKey: ["storage-migration-window-options", revision, page, size],
    queryFn: ({ signal }) => storageMigrationWindowsApi.list(page * size, size, signal),
    enabled,
    retry: false,
  });
  const selectedDetail = useQuery({
    queryKey: ["storage-migration-window", revision, selected?.window_id],
    queryFn: ({ signal }) => storageMigrationWindowsApi.get(selected!.window_id, signal),
    enabled: !!selected,
    retry: false,
  });
  const denied = copyAccessDenied(windows.error) || copyAccessDenied(selectedDetail.error);
  const current = selectedDetail.data ?? selected;
  const valid =
    !selected ||
    (!!current &&
      current.state === "stopped" &&
      !windows.isError &&
      !selectedDetail.isError &&
      !selectedDetail.isFetching);
  useEffect(() => {
    onSelection(selected?.window_id ?? "", valid && !denied);
  }, [selected?.window_id, valid, denied, onSelection]);
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  useEffect(() => {
    if (!windows.data || windows.isFetching) return;
    const last = Math.max(0, Math.ceil(windows.data.total / size) - 1);
    if (page > last) setPage(last);
  }, [windows.data, windows.isFetching, page, size]);
  const options = denied
    ? []
    : (windows.data?.items ?? []).filter((item) => item.state === "stopped");
  const choices =
    selected && !options.some((item) => item.window_id === selected.window_id)
      ? [current ?? selected, ...options]
      : options;
  return (
    <details
      onToggle={(event) => {
        if (event.currentTarget.open) setEnabled(true);
      }}
      className="rounded-md border border-border p-3"
    >
      <summary className="cursor-pointer text-sm font-medium">可选：在迁移窗口内建立清单</summary>
      <div className="mt-3 space-y-3">
        <div>
          <label htmlFor="migration-inventory-window" className="mb-1 block text-sm">
            迁移窗口
          </label>
          <select
            id="migration-inventory-window"
            value={selected?.window_id ?? ""}
            disabled={disabled || denied}
            aria-describedby="migration-inventory-window-hint"
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
            onChange={(event) => {
              const next = choices.find((item) => item.window_id === event.target.value) ?? null;
              onSelection(next?.window_id ?? "", !next);
              setSelected(next);
            }}
          >
            <option value="" disabled hidden />
            {choices.map((item) => (
              <option key={item.window_id} value={item.window_id}>
                {stamp(item.created_at)} · {item.window_id}
              </option>
            ))}
          </select>
          <p
            id="migration-inventory-window-hint"
            className="mt-1 text-xs leading-relaxed text-muted-foreground"
          >
            不选择时沿用普通清单路径。仅可选择已停止登记访问的窗口；窗口不证明完全物理静默。后续复制批次从报告继承窗口绑定。
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={disabled || denied || !selected}
            onClick={() => {
              onSelection("", true);
              setSelected(null);
            }}
          >
            使用普通清单路径
          </Button>
          <Button
            size="sm"
            disabled={disabled || denied || windows.isFetching || selectedDetail.isFetching}
            onClick={() => {
              void windows.refetch();
              if (selected) void selectedDetail.refetch();
            }}
          >
            刷新可用窗口
          </Button>
        </div>
        {selected && !valid && (
          <p role="alert" className="text-xs text-status-danger">
            所选窗口尚未确认可用或已不处于停止状态。请刷新或明确改用普通清单路径。
          </p>
        )}
        {(windows.error || selectedDetail.error) && (
          <p role="alert" className="text-xs text-status-danger">
            窗口读取失败，所选窗口不可提交。
            {migrationWindowError(windows.error ?? selectedDetail.error)}
          </p>
        )}
        {windows.data && !denied && (
          <ListPagination
            label="可选迁移窗口"
            page={page}
            pageSize={size}
            total={windows.data.total}
            loading={windows.isFetching}
            onPageChange={setPage}
            onPageSizeChange={(value) => {
              setSize(value);
              setPage(0);
            }}
          />
        )}
      </div>
    </details>
  );
}
