import { useEffect, useRef, useState, type RefObject } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  MIGRATION_PURPOSES,
  storageMigrationCopiesApi,
  type MigrationCopyCreate,
  type MigrationCopyLimits,
  type MigrationCopyTarget,
  type MigrationHandling,
  type MigrationPurpose,
  type MigrationCopyItem,
} from "@/api/storageMigrationCopies";
import type { StorageMigrationInventoryItem } from "@/api/storageMigrationInventories";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { ListPagination } from "@/components/ui/ListPagination";

export const COPY_HANDLING_LABELS: Record<MigrationHandling, string> = {
  migrate: "复制到目标",
  rebuild: "后续重建",
  retain: "保留源端",
};
export const COPY_LIMIT_FIELDS = [
  ["max_object_bytes", "单对象上限（bytes）", 1073741824, 268435456],
  ["max_source_bytes", "源读取上限（bytes）", 17179869184, 2147483648],
  ["max_upload_bytes", "目标上传上限（bytes）", 17179869184, 2147483648],
  ["max_verify_bytes", "目标核验上限（bytes）", 17179869184, 2147483648],
  ["max_objects", "每次最多处理对象", 100000, 10000],
  ["max_seconds", "每次执行时限（秒）", 3600, 1800],
  ["connect_timeout_seconds", "连接超时（秒）", 60, 10],
  ["read_timeout_seconds", "读取超时（秒）", 60, 60],
] as const;
export function copyAccessDenied(error: unknown) {
  return (
    !!error &&
    typeof error === "object" &&
    "status" in error &&
    [401, 403].includes(Number(error.status))
  );
}

export function MigrationCopyPrepareModal({
  source,
  onClose,
  onPrepared,
  onDenied,
  returnFocusRef,
}: {
  source: StorageMigrationInventoryItem;
  onClose: () => void;
  onPrepared: (item: MigrationCopyItem) => void;
  onDenied: () => void;
  returnFocusRef: RefObject<HTMLElement | null>;
}) {
  const revision = useSessionRevision();
  const capture = useSessionGuard();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<MigrationCopyTarget | null>(null);
  const [handling, setHandling] = useState<Partial<Record<MigrationPurpose, MigrationHandling>>>(
    {},
  );
  const [values, setValues] = useState(
    () =>
      Object.fromEntries(
        COPY_LIMIT_FIELDS.map(([key, , , value]) => [key, String(value)]),
      ) as Record<keyof MigrationCopyLimits, string>,
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const cancel = useRef<HTMLButtonElement>(null);
  const targets = useQuery({
    queryKey: ["storage-migration-copy-targets", revision, source.job_id, page, pageSize],
    queryFn: ({ signal }) =>
      storageMigrationCopiesApi.targets(source.job_id, page * pageSize, pageSize, signal),
    retry: false,
  });
  const denied = copyAccessDenied(targets.error);
  useEffect(() => {
    if (denied) onDenied();
  }, [denied, onDenied]);
  const options = denied ? [] : (targets.data?.items ?? []);
  const choices =
    selected && !options.some((entry) => entry.location_id === selected.location_id)
      ? [selected, ...options]
      : options;
  const limits = Object.fromEntries(
    COPY_LIMIT_FIELDS.map(([key]) => [key, Number(values[key])]),
  ) as unknown as MigrationCopyLimits;
  const valid =
    COPY_LIMIT_FIELDS.every(
      ([key, , max]) =>
        values[key].trim() !== "" &&
        Number.isFinite(limits[key]) &&
        limits[key] > 0 &&
        limits[key] <= max &&
        Number.isSafeInteger(limits[key]),
    ) && MIGRATION_PURPOSES.every(([key]) => !!handling[key]);
  const submit = async () => {
    if (!selected || !valid || busy || denied) return;
    const current = capture();
    setBusy(true);
    setError("");
    const body: MigrationCopyCreate = {
      inventory_job_id: source.job_id,
      target_location_id: selected.location_id,
      handling: handling as Record<MigrationPurpose, MigrationHandling>,
      limits,
    };
    try {
      const response = await storageMigrationCopiesApi.create(body);
      if (current()) onPrepared(response.item);
    } catch (failure) {
      if (current()) {
        setError(verifiedContentError(failure));
        if (copyAccessDenied(failure)) onDenied();
      }
    } finally {
      if (current()) setBusy(false);
    }
  };
  return (
    <Modal
      open
      title="准备初次复制"
      width={680}
      onClose={() => {
        if (!busy) onClose();
      }}
      returnFocusRef={returnFocusRef}
      initialFocusRef={cancel}
      footer={
        <div className="flex flex-wrap justify-end gap-2">
          <Button ref={cancel} disabled={busy} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="primary"
            disabled={!selected || !valid || busy || denied}
            onClick={() => void submit()}
          >
            {busy ? "提交准备任务…" : "准备复制计划"}
          </Button>
        </div>
      }
    >
      <div className="space-y-4 text-sm">
        <section className="rounded-md border border-border p-3">
          <h3 className="text-sm font-semibold">固定源清单</h3>
          <p className="mt-2">
            {new Date(source.created_at).toLocaleString("zh-CN")} ·{" "}
            {source.complete === true ? "清单完整" : "清单不完整，仅处理合格子集"}
          </p>
          <p className="mt-1 break-all text-xs text-muted-foreground">
            SHA-256：{source.report_sha256}
          </p>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            本次只生成配对引用目录和冻结计划，不写入目标。准备后先审阅结果，再明确开始初次复制；不切换绑定或清理源端。
          </p>
        </section>
        <section>
          <label htmlFor="migration-copy-target" className="mb-1 block text-sm font-medium">
            已登记 S3 目标
          </label>
          <select
            id="migration-copy-target"
            value={selected?.location_id ?? ""}
            disabled={busy || denied}
            aria-describedby="migration-copy-target-hint"
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
            onChange={(event) =>
              setSelected(choices.find((entry) => entry.location_id === event.target.value) ?? null)
            }
          >
            <option value="" disabled hidden />
            {choices.map((entry) => (
              <option key={entry.location_id} value={entry.location_id}>
                {entry.name}
              </option>
            ))}
          </select>
          <p
            id="migration-copy-target-hint"
            className="mt-1 text-xs leading-relaxed text-muted-foreground"
          >
            仅列出已启用的全局 S3
            位置。候选不代表能力已验证；目标需启用版本控制，实际执行仍复核当前配置与权限。
          </p>
          {targets.isFetching && (
            <p role="status" className="mt-2 text-xs text-muted-foreground">
              正在读取候选目标…
            </p>
          )}
          {targets.error && (
            <p role="alert" className="mt-2 text-xs text-status-danger">
              {denied ? "当前账号已无权读取复制目标。" : "目标刷新失败，保留上次候选。"}
              {verifiedContentError(targets.error)}
            </p>
          )}
          {!targets.isFetching && !targets.error && options.length === 0 && (
            <p className="mt-2 text-xs text-muted-foreground">
              本页没有可用目标，请先在“存储位置”登记全局 S3 位置。
            </p>
          )}
          {targets.data && !denied && (
            <ListPagination
              label="复制目标"
              page={page}
              pageSize={pageSize}
              total={targets.data.total}
              loading={targets.isFetching}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPage(0);
                setPageSize(size);
              }}
              className="mt-3 px-0 sm:px-0"
            />
          )}
        </section>
        <section aria-label="九用途处理声明">
          <h3 className="text-sm font-semibold">九用途处理声明</h3>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {MIGRATION_PURPOSES.map(([key, label]) => (
              <div key={key}>
                <label htmlFor={`copy-handling-${key}`} className="mb-1 block text-sm">
                  {label}
                </label>
                <select
                  id={`copy-handling-${key}`}
                  value={handling[key] ?? ""}
                  disabled={busy}
                  aria-describedby="migration-copy-handling-hint"
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                  onChange={(event) =>
                    setHandling((previous) => ({
                      ...previous,
                      [key]: event.target.value as MigrationHandling,
                    }))
                  }
                >
                  <option value="" disabled hidden />
                  {Object.entries(COPY_HANDLING_LABELS).map(([value, text]) => (
                    <option key={value} value={value}>
                      {text}
                    </option>
                  ))}
                </select>
              </div>
            ))}
          </div>
          <p
            id="migration-copy-handling-hint"
            className="mt-2 text-xs leading-relaxed text-muted-foreground"
          >
            逐项明确选择。“后续重建”和“保留源端”本轮均不上传、不自动清理；同一物理桶的选择不一致时，该桶会按处理歧义跳过。
          </p>
        </section>
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer text-sm font-medium">高级：复制预算</summary>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            预算固定在计划，每次续跑重新计量本次 I/O；达到上限后保留实际状态，不清除已有复制事实。
          </p>
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            {COPY_LIMIT_FIELDS.map(([key, label, max]) => (
              <div key={key}>
                <label htmlFor={`copy-limit-${key}`} className="mb-1 block text-sm">
                  {label}
                </label>
                <input
                  id={`copy-limit-${key}`}
                  type="number"
                  step={1}
                  value={values[key]}
                  disabled={busy}
                  aria-describedby={`copy-limit-${key}-hint`}
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                  onChange={(event) =>
                    setValues((previous) => ({ ...previous, [key]: event.target.value }))
                  }
                />
                <p id={`copy-limit-${key}-hint`} className="mt-1 text-xs text-muted-foreground">
                  大于 0，最多 {max.toLocaleString()}，填写整数。
                </p>
              </div>
            ))}
          </div>
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
