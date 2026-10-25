import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { asyncJobsApi } from "@/api/asyncJobs";
import {
  storageMigrationInventoriesApi,
  type StorageMigrationInventoryItem,
  type StorageMigrationInventoryLimits,
} from "@/api/storageMigrationInventories";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { AsyncJobDetailModal } from "@/components/jobs/AsyncJobDetailModal";
import { JOB_STATUS_LABEL } from "@/components/jobs/asyncJobPresentation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { MigrationCopyPanel } from "./MigrationCopyPanel";

const LIMIT_FIELDS = [
  ["max_pages", "最多读取页数", 1, 100000, 1000, "每个 LIST 请求计为一页。"],
  ["max_entries", "最多清单条目", 1, 100000, 10000, "包含对象版本、删除标记和未完成上传。"],
  [
    "max_object_bytes",
    "单对象读取上限（bytes）",
    1,
    1073741824,
    268435456,
    "超过上限的对象无法完成摘要核验。",
  ],
  [
    "max_total_bytes",
    "累计读取上限（bytes）",
    1,
    17179869184,
    2147483648,
    "所有对象摘要核验共用此预算。",
  ],
  ["max_seconds", "执行时限（秒）", 1, 3600, 600, "达到时限后停止读取；未封存报告不可下载。"],
  ["read_timeout_seconds", "单次读取超时（秒）", 1, 60, 30, "底层读取结束后才确认取消。"],
  ["page_size", "每页条目上限", 1, 1000, 1000, "源存储可以返回更少的条目。"],
] as const;
const DEFAULTS = Object.fromEntries(
  LIMIT_FIELDS.map(([key, , , , value]) => [key, String(value)]),
) as Record<keyof StorageMigrationInventoryLimits, string>;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function accessDenied(error: unknown): boolean {
  return (
    !!error &&
    typeof error === "object" &&
    "status" in error &&
    [401, 403].includes(Number(error.status))
  );
}

function createdAt(value: string) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN") : "时间未知";
}

export function MigrationInventoryPanel() {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  return role === "super_admin" ? (
    <InventoryOwner key={`${revision}/${role}`} revision={revision} />
  ) : (
    <p className="text-sm text-muted-foreground">迁移准备仅超级管理员可查看。</p>
  );
}

function InventoryOwner({ revision }: { revision: number }) {
  const [params, setParams] = useSearchParams();
  const pageValue = Number(params.get("migration_page") ?? 1);
  const page =
    Number.isSafeInteger(pageValue) && pageValue > 0 && pageValue <= 1000000 ? pageValue - 1 : 0;
  const pageSize = listPageSize(params.get("migration_page_size"));
  const rawJobId = params.get("migration_job") ?? "";
  const jobId = UUID.test(rawJobId) ? rawJobId : null;
  const [values, setValues] = useState({ ...DEFAULTS });
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [forbidden, setForbidden] = useState(false);
  const [copySource, setCopySource] = useState<StorageMigrationInventoryItem | null>(null);
  const closeCopySource = useCallback(() => setCopySource(null), []);
  const denyCopy = useCallback(() => {
    setForbidden(true);
    setError("当前账号已无权读取迁移准备。");
  }, []);
  const capture = useSessionGuard();
  const client = useQueryClient();
  const trigger = useRef<HTMLElement | null>(null);
  const downloads = useRef(new Set<AbortController>());
  useEffect(() => {
    const requests = downloads.current;
    return () => requests.forEach((request) => request.abort());
  }, []);
  const history = useQuery({
    queryKey: ["storage-migration-inventories", revision, pageSize, page],
    queryFn: ({ signal }) => storageMigrationInventoriesApi.list(page * pageSize, pageSize, signal),
    enabled: !forbidden,
    retry: false,
    refetchInterval: (query) =>
      !forbidden &&
      !accessDenied(query.state.error) &&
      query.state.data?.items.some((item) => ["pending", "running"].includes(item.status))
        ? 5000
        : false,
  });
  const denied = forbidden || accessDenied(history.error);
  useEffect(() => {
    if (accessDenied(history.error)) setForbidden(true);
  }, [history.error]);
  const data = denied ? undefined : history.data;
  const limits = Object.fromEntries(
    LIMIT_FIELDS.map(([key]) => [key, Number(values[key])]),
  ) as unknown as StorageMigrationInventoryLimits;
  const valid = LIMIT_FIELDS.every(
    ([key, , min, max]) =>
      values[key].trim() !== "" &&
      Number.isSafeInteger(limits[key]) &&
      limits[key] >= min &&
      limits[key] <= max,
  );
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
  useEffect(() => {
    if (!data || history.isFetching) return;
    const last = Math.max(0, Math.ceil(data.total / pageSize) - 1);
    if (page > last)
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("migration_page", String(last + 1));
          return next;
        },
        { replace: true },
      );
  }, [data, history.isFetching, page, pageSize, setParams]);
  const refresh = () =>
    Promise.all([
      client.invalidateQueries({ queryKey: ["storage-migration-inventories"] }),
      client.invalidateQueries({ queryKey: ["async-jobs"] }),
    ]);
  const act = async (name: string, action: () => Promise<void>) => {
    const current = capture();
    setBusy(name);
    setError("");
    setMessage("");
    try {
      await action();
      if (current()) await refresh();
    } catch (failure) {
      if (current()) {
        setError(verifiedContentError(failure));
        if (accessDenied(failure)) setForbidden(true);
      }
    } finally {
      if (current()) setBusy(null);
    }
  };
  const start = () => {
    const current = capture();
    return act("start", async () => {
      const response = await storageMigrationInventoriesApi.start(limits);
      if (!current()) return;
      patch({ migration_job: response.item.job_id });
      setMessage(
        response.reused ? "已打开现有活动清单任务。" : "清单任务已提交，执行结果以任务状态为准。",
      );
    });
  };
  const redispatch = (item: StorageMigrationInventoryItem) => {
    const current = capture();
    return act(item.job_id, async () => {
      const response = await storageMigrationInventoriesApi.dispatch(item.job_id);
      if (current())
        setMessage(
          response.item.status === "pending"
            ? "已请求重新派发原等待任务，尚不代表已开始执行。"
            : "任务状态已变化，继续使用原任务记录。",
        );
    });
  };
  const cancel = (item: StorageMigrationInventoryItem) => {
    const current = capture();
    return act(item.job_id, async () => {
      await asyncJobsApi.cancel(item.job_id);
      if (current()) setMessage("已提交取消请求，运行中的读取需等待底层收尾。");
    });
  };
  const download = (item: StorageMigrationInventoryItem) => {
    const current = capture();
    return act(item.job_id, async () => {
      const controller = new AbortController();
      downloads.current.add(controller);
      try {
        const blob = await storageMigrationInventoriesApi.report(item.job_id, controller.signal);
        if (!current()) return;
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        try {
          anchor.href = url;
          anchor.download = "storage-migration-inventory.jsonl";
          document.body.appendChild(anchor);
          anchor.click();
        } finally {
          anchor.remove();
          URL.revokeObjectURL(url);
        }
      } finally {
        downloads.current.delete(controller);
      }
    });
  };
  return (
    <section aria-label="迁移准备" className="space-y-4">
      {!denied && (
        <MigrationCopyPanel
          source={copySource}
          onSourceClose={closeCopySource}
          onDenied={denyCopy}
          jobFocusRef={trigger}
        />
      )}
      <Card>
        <div className="space-y-3 p-4">
          <div>
            <h2 className="text-md font-semibold">建立配置源清单</h2>
            <p className="mt-2 text-sm leading-relaxed">
              盘点当前部署配置源的九项平台用途，核验对象内容，为后续迁移准备输入。
            </p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              范围固定为当前配置端点与桶映射，不能选择任意外部位置。已切往其他位置的用途不在本次覆盖内。九用途处理声明均未审定。
            </p>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              此操作读取源存储，不执行复制、切换或物理退役。清单完整也不表示已具备迁移条件。报告随任务保留
              30 天。
            </p>
          </div>
          <details className="rounded-md border border-border p-3">
            <summary className="cursor-pointer text-sm font-medium">高级：读取预算</summary>
            <div className="mt-3 grid gap-4 sm:grid-cols-2">
              {LIMIT_FIELDS.map(([key, label, min, max, , hint]) => (
                <div key={key}>
                  <label htmlFor={`migration-${key}`} className="mb-1 block text-sm">
                    {label}
                  </label>
                  <input
                    id={`migration-${key}`}
                    type="number"
                    min={min}
                    max={max}
                    step={1}
                    value={values[key]}
                    disabled={!!busy || denied}
                    aria-describedby={`migration-${key}-hint`}
                    onChange={(event) =>
                      setValues((previous) => ({ ...previous, [key]: event.target.value }))
                    }
                    className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                  />
                  <p
                    id={`migration-${key}-hint`}
                    className="mt-1 text-xs leading-relaxed text-muted-foreground"
                  >
                    {hint} 范围 {min.toLocaleString()}–{max.toLocaleString()}。
                  </p>
                </div>
              ))}
            </div>
            {!valid && (
              <p role="alert" className="mt-3 text-xs text-status-danger">
                请将所有预算填写为范围内的整数。
              </p>
            )}
          </details>
          <Button
            variant="primary"
            disabled={!valid || !!busy || denied}
            onClick={(event) => {
              trigger.current = event.currentTarget;
              void start();
            }}
          >
            {busy === "start" ? "提交中…" : "建立迁移清单"}
          </Button>
          {message && !denied && (
            <p role="status" className="text-xs text-muted-foreground">
              {message}
            </p>
          )}
          {error && (
            <p role="alert" className="text-xs text-status-danger">
              {error}
            </p>
          )}
        </div>
      </Card>
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2 p-4">
          <h2 className="text-md font-semibold">清单历史</h2>
          <Button
            size="sm"
            disabled={history.isFetching || denied}
            onClick={() => void history.refetch()}
          >
            刷新历史
          </Button>
        </div>
        {history.isFetching && (
          <p role="status" className="px-4 pb-3 text-xs text-muted-foreground">
            正在读取清单历史…
          </p>
        )}
        {history.error && (
          <p role="alert" className="px-4 pb-3 text-xs text-status-danger">
            {denied
              ? "当前账号已无权读取迁移准备。"
              : data
                ? "刷新失败，保留上次清单历史。"
                : "无法读取清单历史。"}
            {verifiedContentError(history.error)}
          </p>
        )}
        {data && (
          <>
            {data.items.length === 0 && (
              <p className="px-4 pb-4 text-sm text-muted-foreground">
                尚无迁移清单，建立后可在此查看结果。
              </p>
            )}
            <ul className="divide-y divide-border">
              {data.items.map((item) => (
                <li key={item.job_id} className="space-y-3 px-4 py-3 text-sm">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <div className="font-medium">配置源清单 · {createdAt(item.created_at)}</div>
                      <p className="mt-1 text-xs text-muted-foreground">
                        {item.complete === true
                          ? "清单完整"
                          : item.complete === false
                            ? "清单不完整"
                            : "清单完整性待确认"}{" "}
                        · {item.report_available ? "报告可下载" : "报告不可下载"}
                      </p>
                    </div>
                    <Badge
                      variant={
                        item.status === "failed"
                          ? "danger"
                          : item.status === "completed"
                            ? "success"
                            : "outline"
                      }
                    >
                      {JOB_STATUS_LABEL[item.status]}
                    </Badge>
                  </div>
                  {item.counts && (
                    <p className="text-xs leading-relaxed text-muted-foreground tabular-nums">
                      桶 {item.counts.buckets} · 对象版本 {item.counts.objects} · 删除标记{" "}
                      {item.counts.delete_markers} · 未完成上传 {item.counts.multipart} · 已核验对象{" "}
                      {item.counts.verified_objects} · 问题 {item.counts.issues}
                    </p>
                  )}
                  {item.counts && (
                    <details>
                      <summary className="cursor-pointer text-xs text-muted-foreground">
                        查看读取统计
                      </summary>
                      <p className="mt-2 text-xs leading-relaxed text-muted-foreground tabular-nums">
                        清单条目 {item.counts.entries} · 读取页数 {item.counts.pages} · 已读取{" "}
                        {item.counts.bytes_read.toLocaleString()} bytes · 已核验{" "}
                        {item.counts.verified_bytes.toLocaleString()} bytes
                      </p>
                    </details>
                  )}
                  {item.error_code && (
                    <p className="break-words text-xs text-status-danger">
                      错误码：{item.error_code}
                    </p>
                  )}
                  {item.cancel_requested && ["pending", "running"].includes(item.status) && (
                    <p className="text-xs text-muted-foreground">已请求取消，等待底层读取收尾。</p>
                  )}
                  {item.status === "pending" && !item.cancel_requested && (
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      等待任务可显式重新派发，始终复用同一任务记录。
                    </p>
                  )}
                  <div className="flex flex-wrap gap-2">
                    {item.report_available && item.report_sha256 && (
                      <Button
                        size="sm"
                        onClick={(event) => {
                          trigger.current = event.currentTarget;
                          setCopySource(item);
                        }}
                      >
                        准备初次复制
                      </Button>
                    )}
                    <Button
                      size="sm"
                      onClick={(event) => {
                        trigger.current = event.currentTarget;
                        patch({ migration_job: item.job_id });
                      }}
                    >
                      查看任务详情
                    </Button>
                    {item.report_available && (
                      <Button size="sm" disabled={!!busy} onClick={() => void download(item)}>
                        下载清单报告
                      </Button>
                    )}
                    {item.status === "pending" && !item.cancel_requested && (
                      <Button size="sm" disabled={!!busy} onClick={() => void redispatch(item)}>
                        重新派发
                      </Button>
                    )}
                    {["pending", "running"].includes(item.status) && (
                      <Button
                        size="sm"
                        disabled={!!busy || item.cancel_requested}
                        onClick={() => void cancel(item)}
                      >
                        取消任务
                      </Button>
                    )}
                  </div>
                </li>
              ))}
            </ul>
            <ListPagination
              label="迁移清单"
              page={page}
              pageSize={pageSize}
              total={data.total}
              loading={history.isFetching}
              onPageChange={(next) => patch({ migration_page: String(next + 1) })}
              onPageSizeChange={(size) =>
                patch({ migration_page_size: String(size), migration_page: "1" })
              }
            />
          </>
        )}
      </Card>
      {jobId && !denied && (
        <AsyncJobDetailModal
          jobId={jobId}
          onClose={() => patch({ migration_job: null })}
          returnFocusRef={trigger}
        />
      )}
    </section>
  );
}
