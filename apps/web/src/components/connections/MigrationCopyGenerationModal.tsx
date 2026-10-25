import { useEffect, useRef, useState, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import {
  MIGRATION_PURPOSES,
  storageMigrationCopiesApi,
  type MigrationCopyItem,
} from "@/api/storageMigrationCopies";
import {
  storageMigrationInventoriesApi,
  type StorageMigrationInventoryItem,
} from "@/api/storageMigrationInventories";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { ListPagination } from "@/components/ui/ListPagination";
import { Modal } from "@/components/ui/Modal";
import {
  COPY_HANDLING_LABELS,
  COPY_LIMIT_FIELDS,
  copyAccessDenied,
} from "./MigrationCopyPrepareModal";

function eligible(item: StorageMigrationInventoryItem, parent: MigrationCopyItem) {
  return (
    item.report_available &&
    !!item.report_sha256 &&
    item.job_id !== parent.source_inventory_job_id &&
    item.report_sha256 !== parent.source_report_sha256 &&
    item.status !== "pending" &&
    item.status !== "running"
  );
}

function generationError(error: unknown) {
  const messages: Record<string, string> = {
    copy_window_changed: "清单与固定父批次的窗口绑定不匹配，请选择该窗口的封存清单。",
    migration_source_read_denied: "源读取未获准，请核实窗口状态与源访问门禁。",
    copy_read_only: "原批次已冻结，请刷新并打开已创建的后续批次。",
    copy_successor_exists: "原批次已有另一份清单的后续批次，请刷新后查看。",
    copy_generation_unchanged: "请选择与原批次不同的封存清单。",
    copy_not_prepared: "原批次尚未准备完成，请先完成原计划准备。",
    copy_active_conflict: "原批次仍有活动任务，请等待任务结束后重试。",
    copy_execution_unsettled: "原批次的读取或上传尚未收尾，请等待或检查 Worker 状态后重试。",
    copy_source_changed: "新清单的来源范围已变化，请选择与原批次同一来源的清单。",
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

export function MigrationCopyGenerationModal({
  parent,
  onClose,
  onPrepared,
  onDenied,
  returnFocusRef,
}: {
  parent: MigrationCopyItem;
  onClose: () => void;
  onPrepared: (item: MigrationCopyItem) => void;
  onDenied: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const cancel = useRef<HTMLButtonElement>(null);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<StorageMigrationInventoryItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const inventories = useQuery({
    queryKey: [
      "storage-migration-generation-inventories",
      revision,
      parent.copy_id,
      page,
      pageSize,
    ],
    queryFn: ({ signal }) => storageMigrationInventoriesApi.list(page * pageSize, pageSize, signal),
    retry: false,
  });
  const denied = copyAccessDenied(inventories.error);
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  useEffect(() => {
    if (!inventories.data || inventories.isFetching) return;
    const last = Math.max(0, Math.ceil(inventories.data.total / pageSize) - 1);
    if (page > last) setPage(last);
    if (selected) {
      const updated = inventories.data.items.find((item) => item.job_id === selected.job_id);
      if (updated && !eligible(updated, parent)) setSelected(null);
    }
  }, [inventories.data, inventories.isFetching, page, pageSize, parent, selected]);
  const options = denied
    ? []
    : (inventories.data?.items ?? []).filter((item) => eligible(item, parent));
  const choices =
    selected && !options.some((item) => item.job_id === selected.job_id)
      ? [selected, ...options]
      : options;
  const submit = async () => {
    if (!selected || !eligible(selected, parent) || busy || denied) return;
    const current = capture();
    setBusy(true);
    setError("");
    try {
      const response = await storageMigrationCopiesApi.generation(parent.copy_id, selected.job_id);
      if (current()) onPrepared(response.item);
    } catch (failure) {
      if (current()) {
        setError(generationError(failure));
        if (copyAccessDenied(failure)) onDenied();
      }
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <Modal
      open
      title="准备后续批次"
      width={700}
      onClose={() => {
        if (!busy) onClose();
      }}
      returnFocusRef={returnFocusRef}
      initialFocusRef={cancel}
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          <Button ref={cancel} disabled={busy} onClick={onClose}>
            返回原批次
          </Button>
          <Button
            variant="primary"
            disabled={!selected || busy || denied}
            onClick={() => void submit()}
          >
            {busy ? "提交准备任务…" : "准备后续批次"}
          </Button>
        </div>
      }
    >
      <div className="space-y-4 text-sm">
        <section className="rounded-md border border-border p-3">
          <h3 className="text-md font-semibold">第 {(parent.generation ?? 1) + 1} 批复制计划</h3>
          <p className="mt-2">目标：{parent.target_name ?? "已登记目标"}</p>
          <p className="mt-1 break-all text-xs text-muted-foreground">
            原批次：{parent.copy_id} · 第 {parent.generation ?? 1} 批
          </p>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            目标、九用途声明和预算继承原批次。提交后原批次只读；先审阅新清单的实际差异，再明确开始增量复制。
          </p>
        </section>
        <section>
          <label htmlFor="migration-generation-inventory" className="mb-1 block font-medium">
            后续封存清单
          </label>
          <select
            id="migration-generation-inventory"
            value={selected?.job_id ?? ""}
            disabled={busy || denied}
            aria-describedby="migration-generation-inventory-hint"
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
            onChange={(event) =>
              setSelected(choices.find((item) => item.job_id === event.target.value) ?? null)
            }
          >
            <option value="" disabled hidden />
            {choices.map((item) => (
              <option key={item.job_id} value={item.job_id}>
                {new Date(item.created_at).toLocaleString("zh-CN")} · {item.job_id.slice(0, 8)}
              </option>
            ))}
          </select>
          <p
            id="migration-generation-inventory-hint"
            className="mt-1 text-xs leading-relaxed text-muted-foreground"
          >
            本页仅列出与原批次不同、可下载的封存清单。不完整清单也可准备合格子集；来源范围仍由服务端复核。
          </p>
          {selected && (
            <div className="mt-3 space-y-1 text-xs text-muted-foreground">
              <p>{selected.complete === true ? "清单完整" : "清单不完整，仅处理合格子集"}</p>
              <p className="break-all">SHA-256：{selected.report_sha256}</p>
              <p className="break-all">
                {selected.window_id
                  ? `后续批次继承窗口 ${selected.window_id} · 绑定修订 ${selected.window_revision ?? "未知"}；固定父批次与目标由服务端复核。`
                  : "普通清单路径 · 后续批次不绑定迁移窗口"}
              </p>
            </div>
          )}
          {inventories.isFetching && (
            <p role="status" className="mt-2 text-xs text-muted-foreground">
              正在读取封存清单…
            </p>
          )}
          {inventories.error && (
            <p role="alert" className="mt-2 text-xs text-status-danger">
              {denied ? "当前账号已无权读取迁移清单。" : "清单刷新失败，保留上次候选。"}
              {verifiedContentError(inventories.error)}
            </p>
          )}
          {!inventories.isFetching && !inventories.error && options.length === 0 && (
            <p className="mt-2 text-xs text-muted-foreground">
              本页没有符合条件的新清单。可翻页查找，或先为同一来源创建清单任务。
            </p>
          )}
          {inventories.data && !denied && (
            <ListPagination
              label="后续清单"
              page={page}
              pageSize={pageSize}
              total={inventories.data.total}
              loading={inventories.isFetching}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPage(0);
                setPageSize(size);
              }}
              className="mt-3 px-0 sm:px-0"
            />
          )}
        </section>
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer font-medium">继承的九用途处理声明（只读）</summary>
          <dl className="mt-3 grid gap-2 sm:grid-cols-2">
            {MIGRATION_PURPOSES.map(([key, label]) => (
              <div key={key} className="flex flex-wrap justify-between gap-2 text-xs">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="m-0">{COPY_HANDLING_LABELS[parent.handling[key]]}</dd>
              </div>
            ))}
          </dl>
        </details>
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer font-medium">继承的复制预算（只读）</summary>
          <dl className="mt-3 grid gap-2 sm:grid-cols-2">
            {COPY_LIMIT_FIELDS.map(([key, label]) => (
              <div key={key} className="flex flex-wrap justify-between gap-2 text-xs">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="m-0 tabular-nums">{parent.limits[key].toLocaleString("zh-CN")}</dd>
              </div>
            ))}
          </dl>
        </details>
        {error && (
          <p role="alert" className="text-xs text-status-danger">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
