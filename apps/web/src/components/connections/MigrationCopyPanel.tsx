import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { asyncJobsApi } from "@/api/asyncJobs";
import { ApiError } from "@/api/client";
import {
  MIGRATION_PURPOSES,
  storageMigrationCopiesApi,
  type MigrationCopyItem,
  type MigrationCopyAttemptOperation,
} from "@/api/storageMigrationCopies";
import type { StorageMigrationInventoryItem } from "@/api/storageMigrationInventories";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { usePermissions } from "@/hooks/usePermissions";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Modal } from "@/components/ui/Modal";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { JOB_STATUS_LABEL } from "@/components/jobs/asyncJobPresentation";
import { cacheBytes } from "@/utils/cachePolicy";
import {
  MigrationCopyPrepareModal,
  COPY_HANDLING_LABELS,
  copyAccessDenied,
} from "./MigrationCopyPrepareModal";
import { MigrationCopyGenerationModal } from "./MigrationCopyGenerationModal";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DECISIONS: Record<string, string> = {
  copy: "纳入复制",
  retain: "保留源端",
  rebuild: "后续重建",
  delete_marker: "删除标记",
  handling_ambiguous: "处理歧义",
  source_unverified: "源未核验",
  object_byte_budget: "超过单对象预算",
  source_identity_conflict: "源身份冲突",
};
const GENERATION_COUNTS = [
  ["reused_objects", "复用映射"],
  ["new_objects", "新增对象"],
  ["mutable_recopies", "可变对象重复制"],
  ["source_identity_conflicts", "源身份冲突"],
  ["previous_only_objects", "本次未观察到"],
  ["delete_markers", "删除标记"],
  ["history_records", "保留历史记录"],
] as const;
const ACTIVE = new Set(["pending", "running"]);
function readOnly(item: MigrationCopyItem) {
  return item.read_only === true || !!item.successor_copy_id;
}
function subset(item: MigrationCopyItem) {
  if (!item.prepared) return "尚无封存计划";
  if (item.decisions && (item.decisions.copy ?? 0) === 0) return "无可复制对象";
  return item.copy_subset_complete === true ? "可复制子集已核验" : "可复制子集尚未全部核验";
}
function pageIndex(value: string | null) {
  const n = Number(value ?? 1);
  return Number.isSafeInteger(n) && n > 0 && n <= 1000000 ? n - 1 : 0;
}
function stamp(value: string) {
  return Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString("zh-CN") : "时间未知";
}
function copyError(error: unknown) {
  if (
    error instanceof ApiError &&
    error.detailRaw &&
    typeof error.detailRaw === "object" &&
    "code" in error.detailRaw &&
    error.detailRaw.code === "copy_execution_unsettled"
  )
    return "原执行结束尚未确认，仍保留原任务。请等待或检查 Worker 状态后重试。";
  if (
    error instanceof ApiError &&
    error.detailRaw &&
    typeof error.detailRaw === "object" &&
    "code" in error.detailRaw &&
    String(error.detailRaw.code).endsWith("selection_changed")
  )
    return "已选失败项状态已变化，请刷新失败列表并重新选择。";
  return verifiedContentError(error);
}
interface Props {
  source: StorageMigrationInventoryItem | null;
  onSourceClose: () => void;
  onDenied: () => void;
  jobFocusRef: MutableRefObject<HTMLElement | null>;
}

export function MigrationCopyPanel(props: Props) {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  return role === "super_admin" ? (
    <CopyOwner key={`${revision}/${role}`} revision={revision} {...props} />
  ) : null;
}

function CopyOwner({
  source,
  onSourceClose,
  onDenied,
  jobFocusRef,
  revision,
}: Props & { revision: number }) {
  const [params, setParams] = useSearchParams();
  const page = pageIndex(params.get("migration_copy_page"));
  const pageSize = listPageSize(params.get("migration_copy_page_size"));
  const rawId = params.get("migration_copy") ?? "";
  const copyId = UUID.test(rawId) ? rawId : "";
  const jobOpen = !!params.get("migration_job");
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [forbidden, setForbidden] = useState(false);
  const [generationParent, setGenerationParent] = useState("");
  const [focusGeneration, setFocusGeneration] = useState(false);
  const trigger = useRef<HTMLElement | null>(null);
  const generationTrigger = useRef<HTMLButtonElement | null>(null);
  const downloads = useRef(new Set<AbortController>());
  const selection = useRef(copyId);
  selection.current = copyId;
  useEffect(() => {
    setError("");
    setBusy(false);
    setGenerationParent("");
    setFocusGeneration(false);
    downloads.current.forEach((controller) => controller.abort());
  }, [copyId]);
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
    queryKey: ["storage-migration-copies", revision, page, pageSize],
    queryFn: ({ signal }) => storageMigrationCopiesApi.list(page * pageSize, pageSize, signal),
    enabled: !forbidden,
    retry: false,
    refetchInterval: (query) =>
      !forbidden &&
      !copyAccessDenied(query.state.error) &&
      query.state.data?.items.some((item) => ACTIVE.has(item.status ?? ""))
        ? 5000
        : false,
  });
  const detail = useQuery({
    queryKey: ["storage-migration-copy", revision, copyId],
    queryFn: ({ signal }) => storageMigrationCopiesApi.get(copyId, signal),
    enabled: !!copyId && !forbidden,
    retry: false,
    refetchInterval: (query) =>
      !forbidden &&
      !copyAccessDenied(query.state.error) &&
      ACTIVE.has(query.state.data?.status ?? "")
        ? 5000
        : false,
  });
  const denied = forbidden || copyAccessDenied(history.error) || copyAccessDenied(detail.error);
  const deny = () => {
    setForbidden(true);
    onDenied();
  };
  useEffect(() => {
    if (copyAccessDenied(history.error) || copyAccessDenied(detail.error)) {
      setForbidden(true);
      onDenied();
    }
  }, [history.error, detail.error, onDenied]);
  useEffect(() => {
    const controllers = downloads.current;
    return () => controllers.forEach((controller) => controller.abort());
  }, []);
  const data = denied ? undefined : history.data;
  const item =
    denied ||
    (detail.error &&
      typeof detail.error === "object" &&
      "status" in detail.error &&
      Number(detail.error.status) === 404)
      ? undefined
      : detail.data;
  const preparingGeneration =
    !!item &&
    generationParent === copyId &&
    item.prepared &&
    !ACTIVE.has(item.status ?? "") &&
    !readOnly(item);
  useEffect(() => {
    if (!data || history.isFetching) return;
    const last = Math.max(0, Math.ceil(data.total / pageSize) - 1);
    if (page > last)
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("migration_copy_page", String(last + 1));
          return next;
        },
        { replace: true },
      );
  }, [data, history.isFetching, page, pageSize, setParams]);
  const refresh = () =>
    Promise.all(
      [
        ["storage-migration-copies"],
        ["storage-migration-copy"],
        ["storage-migration-copy-failures"],
        ["async-jobs"],
      ].map((queryKey) => client.invalidateQueries({ queryKey })),
    );
  const openJob = (next: MigrationCopyItem) => {
    if (trigger.current?.isConnected) jobFocusRef.current = trigger.current;
    patch({ migration_copy: next.copy_id, migration_job: next.job_id });
  };
  const act = async (work: () => Promise<void>) => {
    const session = capture();
    const current = () => session() && selection.current === copyId;
    setBusy(true);
    setError("");
    try {
      await work();
      if (current()) await refresh();
    } catch (failure) {
      if (current()) {
        setError(copyError(failure));
        if (copyAccessDenied(failure)) deny();
      }
    } finally {
      if (current()) setBusy(false);
    }
  };
  const attempt = (
    entry: MigrationCopyItem,
    operation: MigrationCopyAttemptOperation,
    retry = false,
    ids?: string[],
  ) => {
    const session = capture();
    const current = () => session() && selection.current === copyId;
    return act(async () => {
      const response = await storageMigrationCopiesApi.attempt(
        entry.copy_id,
        operation,
        retry,
        ids,
      );
      if (current()) openJob(response.item);
    });
  };
  const dispatch = (entry: MigrationCopyItem) => {
    const session = capture();
    const current = () => session() && selection.current === copyId;
    return act(async () => {
      const response = await storageMigrationCopiesApi.dispatch(entry.copy_id);
      if (current()) openJob(response.item);
    });
  };
  const cancel = (entry: MigrationCopyItem) =>
    act(async () => {
      if (entry.job_id) await asyncJobsApi.cancel(entry.job_id);
    });
  const download = (entry: MigrationCopyItem) => {
    const session = capture();
    const current = () => session() && selection.current === copyId;
    return act(async () => {
      const controller = new AbortController();
      downloads.current.add(controller);
      try {
        const blob = await storageMigrationCopiesApi.checkpoint(entry.copy_id, controller.signal);
        if (!current()) return;
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        try {
          anchor.href = url;
          anchor.download = "storage-migration-copy-checkpoint.sqlite3";
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
    <>
      <Card>
        <section aria-label="复制批次">
          <div className="flex flex-wrap items-center justify-between gap-2 p-4">
            <div>
              <h2 className="text-md font-semibold">复制批次</h2>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                从封存清单准备初次计划，或沿原批次准备后续清单；审阅后再开始复制。仅复制合格子集，不执行切换或退役。
              </p>
            </div>
            <Button
              size="sm"
              disabled={history.isFetching || denied}
              onClick={() => void history.refetch()}
            >
              刷新复制计划
            </Button>
          </div>
          {history.isFetching && (
            <p role="status" className="px-4 pb-3 text-xs text-muted-foreground">
              正在读取复制计划…
            </p>
          )}
          {history.error && (
            <p role="alert" className="px-4 pb-3 text-xs text-status-danger">
              {denied
                ? "当前账号已无权读取复制计划。"
                : data
                  ? "刷新失败，保留上次复制计划。"
                  : "无法读取复制计划。"}
              {verifiedContentError(history.error)}
            </p>
          )}
          {data && (
            <>
              {data.items.length === 0 && (
                <p className="px-4 pb-4 text-sm text-muted-foreground">
                  尚无复制计划。请从封存清单选择“准备初次复制”。
                </p>
              )}
              <ul className="divide-y divide-border">
                {data.items.map((entry) => (
                  <li key={entry.copy_id} className="space-y-2 px-4 py-3 text-sm">
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div>
                        <div className="font-medium">
                          {entry.target_name ?? "已登记目标"} · {stamp(entry.created_at)}
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          第 {entry.generation ?? 1} 批 · {subset(entry)}
                          {entry.status
                            ? ` · 最近任务：${JOB_STATUS_LABEL[entry.status]}`
                            : " · 当前无关联任务"}
                        </p>
                      </div>
                      <Badge variant="outline">
                        {readOnly(entry)
                          ? "原批次只读"
                          : entry.prepared
                            ? "计划已封存"
                            : "准备计划"}
                      </Badge>
                    </div>
                    {entry.counts && <CopyCounts item={entry} />}
                    <Button
                      size="sm"
                      onClick={(event) => {
                        trigger.current = event.currentTarget;
                        patch({ migration_copy: entry.copy_id, migration_job: null });
                      }}
                    >
                      审阅复制计划
                    </Button>
                  </li>
                ))}
              </ul>
              <ListPagination
                label="复制计划"
                page={page}
                pageSize={pageSize}
                total={data.total}
                loading={history.isFetching}
                onPageChange={(next) => patch({ migration_copy_page: String(next + 1) })}
                onPageSizeChange={(size) =>
                  patch({ migration_copy_page: "1", migration_copy_page_size: String(size) })
                }
              />
            </>
          )}
        </section>
      </Card>
      {source && !denied && (
        <MigrationCopyPrepareModal
          key={source.job_id}
          source={source}
          onClose={onSourceClose}
          onDenied={deny}
          returnFocusRef={jobFocusRef}
          onPrepared={(prepared) => {
            onSourceClose();
            patch({ migration_copy: prepared.copy_id, migration_job: prepared.job_id });
            void refresh();
          }}
        />
      )}
      {copyId && !jobOpen && !source && !denied && preparingGeneration && item && (
        <MigrationCopyGenerationModal
          key={item.copy_id}
          parent={item}
          returnFocusRef={trigger}
          onClose={() => {
            setGenerationParent("");
            setFocusGeneration(true);
          }}
          onDenied={deny}
          onPrepared={(prepared) => {
            if (selection.current !== item.copy_id) return;
            setGenerationParent("");
            setFocusGeneration(false);
            openJob(prepared);
            void refresh();
          }}
        />
      )}
      {copyId && !jobOpen && !source && !denied && !preparingGeneration && (
        <Modal
          open
          title={(item?.generation ?? 1) > 1 ? "审阅后续复制批次" : "审阅初次复制计划"}
          width={700}
          onClose={() => patch({ migration_copy: null })}
          returnFocusRef={trigger}
          initialFocusRef={focusGeneration ? generationTrigger : undefined}
        >
          {detail.isFetching && (
            <p role="status" className="mb-3 text-xs text-muted-foreground">
              正在读取计划状态…
            </p>
          )}
          {detail.error && (
            <p role="alert" className="mb-3 text-xs text-status-danger">
              {item ? "刷新失败，保留上次计划状态。" : "无法读取此复制计划。"}
              {verifiedContentError(detail.error)}
            </p>
          )}
          {error && (
            <p role="alert" className="mb-3 text-xs text-status-danger">
              {error}
            </p>
          )}
          {item && (
            <CopyReview
              key={item.copy_id}
              item={item}
              revision={revision}
              busy={busy}
              onAttempt={(operation, retry, ids) => void attempt(item, operation, retry, ids)}
              onDispatch={() => void dispatch(item)}
              onCancel={() => void cancel(item)}
              onJob={() => openJob(item)}
              onDownload={() => void download(item)}
              onDenied={deny}
              generationTrigger={generationTrigger}
              onGeneration={() => {
                setFocusGeneration(false);
                setGenerationParent(item.copy_id);
              }}
              onNavigate={(id) => patch({ migration_copy: id, migration_job: null })}
            />
          )}
        </Modal>
      )}
    </>
  );
}

function CopyCounts({ item }: { item: MigrationCopyItem }) {
  const c = item.counts;
  return c ? (
    <p className="text-xs leading-relaxed text-muted-foreground tabular-nums">
      {(item.generation ?? 1) > 1 ? "本批次累计" : "计划累计"}：已核验 {c.verified} · 待处理{" "}
      {c.pending} · 写入中 {c.writing} · 结果不明 {c.uncertain} · 失败 {c.failed} · 跳过 {c.skipped}
    </p>
  ) : null;
}

function CopyReview({
  item,
  revision,
  busy,
  onAttempt,
  onDispatch,
  onCancel,
  onJob,
  onDownload,
  onDenied,
  generationTrigger,
  onGeneration,
  onNavigate,
}: {
  item: MigrationCopyItem;
  revision: number;
  busy: boolean;
  onAttempt: (op: MigrationCopyAttemptOperation, retry?: boolean, ids?: string[]) => void;
  onDispatch: () => void;
  onCancel: () => void;
  onJob: () => void;
  onDownload: () => void;
  onDenied: () => void;
  generationTrigger: MutableRefObject<HTMLButtonElement | null>;
  onGeneration: () => void;
  onNavigate: (id: string) => void;
}) {
  const active = ACTIVE.has(item.status ?? "");
  const frozen = readOnly(item);
  const generation = item.generation ?? 1;
  const c = item.counts;
  const [failuresOpen, setFailuresOpen] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const failures = useQuery({
    queryKey: ["storage-migration-copy-failures", revision, item.copy_id, page, pageSize],
    queryFn: ({ signal }) =>
      storageMigrationCopiesApi.failedObjects(item.copy_id, page * pageSize, pageSize, signal),
    enabled: failuresOpen && !frozen,
    retry: false,
  });
  useEffect(() => {
    if (copyAccessDenied(failures.error)) onDenied();
  }, [failures.error, onDenied]);
  const failureData = copyAccessDenied(failures.error) ? undefined : failures.data;
  const unfinished = !!c && c.pending + c.writing + c.uncertain > 0;
  return (
    <div className="space-y-4 text-sm">
      <section className="rounded-md border border-border p-3">
        <h3 className="text-md font-semibold">{item.target_name ?? "已登记目标"}</h3>
        <p className="mt-1 text-xs text-muted-foreground">
          第 {generation} 批{frozen ? " · 原批次只读" : ""}
        </p>
        <p className="mt-2">{subset(item)}</p>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          准备成功只代表计划可审阅。源清单{item.source_complete ? "完整" : "不完整"}
          ；未执行绑定切换、源端删除或重建。
        </p>
        <p className="mt-2 break-all text-xs text-muted-foreground">
          源 SHA-256：{item.source_report_sha256}
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          创建：{stamp(item.created_at)} · 更新：{stamp(item.updated_at)}
        </p>
        {!item.source_inventory_job_id && (
          <p className="mt-1 text-xs text-muted-foreground">
            原清单任务引用已过期，固定输入与复制计划仍保留。
          </p>
        )}
      </section>
      {(item.parent_copy_id || item.successor_copy_id || frozen) && (
        <section aria-label="批次关系" className="space-y-2">
          {frozen && (
            <p className="text-xs leading-relaxed text-muted-foreground">
              已预留后续批次，原批次保持只读；可查看原任务和下载检查点。
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            {item.parent_copy_id && (
              <Button size="sm" onClick={() => onNavigate(item.parent_copy_id!)}>
                查看原批次
              </Button>
            )}
            {item.successor_copy_id && (
              <Button size="sm" onClick={() => onNavigate(item.successor_copy_id!)}>
                查看后续批次
              </Button>
            )}
          </div>
          {item.parent_evidence_sha256 && (
            <p className="break-all text-xs text-muted-foreground">
              原批次证据 SHA-256：{item.parent_evidence_sha256}
            </p>
          )}
        </section>
      )}
      {item.generation_summary && (
        <section aria-label="后续清单差异">
          <h3 className="text-sm font-semibold">后续清单差异</h3>
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            {GENERATION_COUNTS.map(([key, label]) => (
              <div key={key} className="flex flex-wrap justify-between gap-2">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="m-0 tabular-nums">{item.generation_summary![key]}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            复用映射保留原目标创建身份，仍须在本批次读回准确版本后才计为已核验。本次未观察到、删除标记和历史记录均不表示源端已删除，也不代表可以切换。
          </p>
        </section>
      )}
      {item.counts && <CopyCounts item={item} />}
      {item.selected_count !== null && (
        <p className="text-xs text-muted-foreground">
          最近选定重试：{item.selected_verified_count ?? 0} / {item.selected_count} 项已核验
          {item.attempt_subset_complete === true
            ? "，选定子集已完成。"
            : "，选定子集尚未全部核验。"}{" "}
          整体计划状态以上方累计为准。
        </p>
      )}
      {item.error_code && (
        <p role="alert" className="break-words text-xs text-status-danger">
          错误码：{item.error_code}
        </p>
      )}
      {item.cancel_requested && active && (
        <p role="status" className="text-xs text-muted-foreground">
          已请求取消，等待原生读取、上传与资源收尾。
        </p>
      )}
      <section>
        <h3 className="text-sm font-semibold">范围与处理结果</h3>
        <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
          引用匹配未知不代表孤儿对象。清单或目录未覆盖的范围不会因复制计数完成而变为已核验。
        </p>
        {item.catalog_summary && (
          <p className="mt-2 text-xs text-muted-foreground">
            引用目录：{item.catalog_summary.complete ? "完整" : "不完整"} · 枚举
            {item.catalog_summary.enumeration_complete ? "完整" : "未完整"} · 定位
            {item.catalog_summary.resolution_complete ? "完整" : "未完整"}
          </p>
        )}
        {item.source_summary && (
          <p className="mt-1 text-xs text-muted-foreground">
            源对象 {item.source_summary.objects} · 删除标记 {item.source_summary.delete_markers} ·
            未核验 {item.source_summary.unverified_objects} · 超限{" "}
            {item.source_summary.over_object_limit} · 准确引用匹配{" "}
            {item.source_summary.exact_catalog_matches}
          </p>
        )}
        {item.decisions && (
          <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-2 text-xs">
            {Object.entries(item.decisions).map(([key, value]) => (
              <div key={key} className="flex flex-wrap justify-between gap-2">
                <dt className="text-muted-foreground">{DECISIONS[key] ?? "其他处理"}</dt>
                <dd className="m-0 tabular-nums">{value}</dd>
              </div>
            ))}
          </dl>
        )}
      </section>
      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer text-sm font-medium">查看九用途处理声明</summary>
        <dl className="mt-3 grid gap-2 sm:grid-cols-2">
          {MIGRATION_PURPOSES.map(([key, label]) => (
            <div key={key} className="flex flex-wrap justify-between gap-2 text-xs">
              <dt className="text-muted-foreground">{label}</dt>
              <dd className="m-0">{COPY_HANDLING_LABELS[item.handling[key]]}</dd>
            </div>
          ))}
        </dl>
      </details>
      {item.io && (
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer text-sm font-medium">最近执行的读取与上传</summary>
          <p className="mt-2 text-xs text-muted-foreground">
            本次处理 {item.io.objects} 项 · 源读取 {cacheBytes(item.io.source_bytes)} · 目标上传{" "}
            {cacheBytes(item.io.upload_bytes)} · 目标核验 {cacheBytes(item.io.verify_bytes)}。本次
            I/O 与计划累计对象状态分别计量。
          </p>
        </details>
      )}
      {!frozen && item.prepared && (c?.failed ?? 0) > 0 && (
        <details
          className="rounded-md border border-border p-3"
          open={failuresOpen}
          onToggle={(event) => setFailuresOpen(event.currentTarget.open)}
        >
          <summary className="cursor-pointer text-sm font-medium">选择失败对象重试</summary>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            只处理已选失败项，其他待处理与结果不明项保持。对象仅展示摘要
            ID；完整定位保存在私密检查点中。最多选择 1,000 项。
          </p>
          {failures.error && (
            <p role="alert" className="mt-2 text-xs text-status-danger">
              读取失败对象失败，保留上次结果。{verifiedContentError(failures.error)}
            </p>
          )}
          <Button
            size="sm"
            disabled={failures.isFetching || busy}
            onClick={() => {
              setSelected(new Set());
              void failures.refetch();
            }}
          >
            刷新失败对象
          </Button>
          <ul className="mt-3 space-y-2">
            {failureData?.items.map((entry) => (
              <li key={entry.object_id}>
                <label className="flex items-start gap-2 text-xs">
                  <input
                    type="checkbox"
                    className="mt-0.5"
                    checked={selected.has(entry.object_id)}
                    disabled={
                      busy || active || (!selected.has(entry.object_id) && selected.size >= 1000)
                    }
                    onChange={(event) =>
                      setSelected((previous) => {
                        const next = new Set(previous);
                        if (event.target.checked) next.add(entry.object_id);
                        else next.delete(entry.object_id);
                        return next;
                      })
                    }
                  />
                  <span className="min-w-0">
                    <span className="block break-all">{entry.object_id}</span>
                    <span className="mt-1 block text-muted-foreground">
                      {cacheBytes(entry.size_bytes)} · {entry.error_code ?? "错误码未提供"}
                    </span>
                  </span>
                </label>
              </li>
            ))}
          </ul>
          {failureData && (
            <ListPagination
              label="失败对象"
              page={page}
              pageSize={pageSize}
              total={failureData.total}
              loading={failures.isFetching}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPage(0);
                setPageSize(size);
              }}
              className="mt-3 px-0 sm:px-0"
            />
          )}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-xs text-muted-foreground">已选 {selected.size} 项</span>
            <Button
              size="sm"
              disabled={busy || active || selected.size === 0 || !!failures.error}
              onClick={() => onAttempt("run", true, [...selected])}
            >
              仅重试已选失败项
            </Button>
            <Button
              size="sm"
              disabled={selected.size === 0 || busy}
              onClick={() => setSelected(new Set())}
            >
              清空选择
            </Button>
          </div>
        </details>
      )}
      <div className="flex flex-wrap gap-2">
        {!frozen && !active && !item.prepared && (
          <Button variant="primary" disabled={busy} onClick={() => onAttempt("prepare")}>
            重试准备
          </Button>
        )}
        {!frozen && !active && item.prepared && unfinished && (
          <Button variant="primary" disabled={busy} onClick={() => onAttempt("run")}>
            {item.operation === "prepare"
              ? generation > 1
                ? "开始增量复制"
                : "开始初次复制"
              : "继续未完成项"}
          </Button>
        )}
        {!frozen && !active && item.prepared && (c?.failed ?? 0) > 0 && (
          <Button disabled={busy} onClick={() => onAttempt("run", true)}>
            续跑并重试全部失败项
          </Button>
        )}
        {!frozen && !active && item.prepared && !!c && c.verified + c.failed + c.uncertain > 0 && (
          <Button disabled={busy} onClick={() => onAttempt("verify")}>
            重新核验
          </Button>
        )}
        {!frozen && item.status === "pending" && !item.cancel_requested && (
          <Button disabled={busy} onClick={onDispatch}>
            重投等待任务
          </Button>
        )}
        {!frozen && active && item.job_id && (
          <Button disabled={busy || item.cancel_requested} onClick={onCancel}>
            取消当前任务
          </Button>
        )}
        {item.job_id && (
          <Button disabled={busy} onClick={onJob}>
            查看本次任务
          </Button>
        )}
        {item.checkpoint_available && (
          <Button disabled={busy} onClick={onDownload}>
            下载私密检查点
          </Button>
        )}
        {!frozen && !active && item.prepared && (
          <Button ref={generationTrigger} disabled={busy} onClick={onGeneration}>
            准备后续批次
          </Button>
        )}
      </div>
      {!frozen && item.status === "running" && (
        <details className="rounded-md border border-border p-3">
          <summary className="cursor-pointer text-sm font-medium">高级：检查中断执行</summary>
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            由后端确认旧进程已结束后，沿原计划和原重试范围恢复。仍活跃或无法确认结束时保留原任务，不并行启动第二个执行。
          </p>
          <Button className="mt-3" size="sm" disabled={busy} onClick={() => onAttempt("recover")}>
            检查并恢复中断执行
          </Button>
        </details>
      )}
      {item.status && (
        <p className="text-xs text-muted-foreground">
          {item.operation === "prepare"
            ? "准备计划"
            : item.operation === "verify"
              ? "只读重新核验"
              : generation > 1
                ? "增量复制执行"
                : "初次复制执行"}{" "}
          · {JOB_STATUS_LABEL[item.status]}
          {!item.job_id ? " · 原任务记录已清理，仍可按保存计划操作" : ""}
        </p>
      )}
    </div>
  );
}
