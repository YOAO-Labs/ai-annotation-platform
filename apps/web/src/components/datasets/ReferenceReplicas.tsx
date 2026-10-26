import { useEffect, useId, useRef, useState } from "react";
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import {
  referenceReplicasApi as api,
  type ReferenceReplicaCandidate,
  type ReferenceReplicaOut,
} from "@/api/referenceReplicas";
import { Button } from "@/components/ui/Button";
import { ListPagination } from "@/components/ui/ListPagination";
import { useDatasetOwner } from "@/hooks/useDatasets";
import { useSessionRevision } from "@/hooks/useSessionGuard";

const field =
  "w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const agentStates: Record<string, string> = {
  online: "在线",
  offline: "离线",
  degraded: "暂不可用",
  pending: "待配对",
  revoked: "授权已撤销",
  expired: "凭据已过期",
  unavailable: "暂不可用",
};
const kinds: Record<string, string> = {
  agent: "跨站点 Agent",
  filesystem: "服务器目录",
  s3: "对象存储",
};

function errorMessage(error: unknown) {
  const code =
    error instanceof ApiError && error.detailRaw && typeof error.detailRaw === "object"
      ? String((error.detailRaw as { code?: unknown }).code ?? "")
      : "";
  const messages: Record<string, string> = {
    dataset_archived: "数据集已归档，备用来源保持只读。",
    reference_replica_exists: "该备用来源已关联，请刷新列表。",
    reference_replica_content_mismatch: "两张图片的内容记录不一致，无法关联。请重新核对候选。",
    reference_replica_requires_accepted_revision: "来源修订尚未接纳，请先在来源与同步页完成核对。",
    reference_replica_changed: "备用来源关联已变化，请刷新后重试。",
    access_revoked: "当前已无管理权限，请刷新后核对。",
    api_key_authority_revoked: "当前访问权限已变化，请重新登录后核对。",
  };
  if (messages[code]) return messages[code];
  if (error instanceof ApiError && error.status === 404)
    return "样本或备用来源已不可用，请刷新列表。";
  if (error instanceof ApiError && error.status === 403) return "当前已无管理权限，请刷新后核对。";
  if (error instanceof ApiError && error.status === 429) return "备用来源服务繁忙，请稍后重试。";
  return "备用来源操作未完成，请刷新列表后重试。";
}

function SourceInfo({ source }: { source: ReferenceReplicaCandidate | ReferenceReplicaOut }) {
  return (
    <div className="min-w-0 space-y-1">
      <p className="break-words text-sm font-medium">{source.source_name}</p>
      <p className="break-words text-xs leading-relaxed text-muted-foreground">
        {source.location_name} · {kinds[source.location_kind] ?? "存储位置"}
        {source.site_name && ` · ${source.site_name}`}
        {source.agent_state && ` · Agent ${agentStates[source.agent_state] ?? "状态待核对"}`}
      </p>
    </div>
  );
}

function ReplicaRow({
  replica,
  archived,
  busy,
  onUpdate,
  onRemove,
}: {
  replica: ReferenceReplicaOut;
  archived: boolean;
  busy: boolean;
  onUpdate: (priority: number) => void;
  onRemove: () => void;
}) {
  const [priority, setPriority] = useState(String(replica.priority));
  const number = priority.trim() === "" ? NaN : Number(priority);
  const valid = Number.isInteger(number) && number >= 0 && number <= 1000;
  return (
    <li className="space-y-2 py-3">
      <SourceInfo source={replica} />
      {archived ? (
        <p className="text-xs text-muted-foreground">优先级：{replica.priority}</p>
      ) : (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (valid && !busy && number !== replica.priority) onUpdate(number);
          }}
        >
          <label className="grid w-28 gap-1 text-xs text-muted-foreground">
            优先级
            <input
              aria-label={`${replica.source_name}优先级`}
              className={field}
              type="number"
              min={0}
              max={1000}
              step={1}
              required
              disabled={busy}
              value={priority}
              onChange={(event) => setPriority(event.target.value)}
            />
          </label>
          <Button size="sm" type="submit" disabled={busy || !valid || number === replica.priority}>
            保存优先级
          </Button>
          <Button size="sm" type="button" disabled={busy} onClick={onRemove}>
            解除关联
          </Button>
        </form>
      )}
    </li>
  );
}

interface Props {
  itemId: string;
  archived: boolean;
  onChanged?: () => Promise<unknown>;
}

export function ReferenceReplicas(props: Props) {
  const revision = useSessionRevision();
  return (
    <ReferenceReplicasOwner key={`${revision}/${props.itemId}/${props.archived}`} {...props} />
  );
}

function ReferenceReplicasOwner({ itemId, archived, onChanged }: Props) {
  const owner = useDatasetOwner();
  const client = useQueryClient();
  const hint = useId();
  const [open, setOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(10);
  const [candidatePage, setCandidatePage] = useState(0);
  const [candidateSize, setCandidateSize] = useState(10);
  const [newPriority, setNewPriority] = useState("100");
  const [busy, setBusy] = useState(false);
  const [serverArchived, setServerArchived] = useState(false);
  const readOnly = archived || serverArchived;
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => pending.current?.abort(), []);
  const listKey = ["reference-replicas", itemId, ...owner.key];
  const candidateKey = ["reference-replica-candidates", itemId, ...owner.key];
  const replicas = useQuery({
    queryKey: [...listKey, page, pageSize],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const result = await api.list(itemId, { limit: pageSize, offset: page * pageSize }, signal);
      owner.assert(signal);
      if (result.items.some((replica) => replica.item_id !== itemId))
        throw new Error("样本归属已变化");
      return result;
    },
    enabled: open && owner.enabled,
    placeholderData: keepPreviousData,
  });
  const candidates = useQuery({
    queryKey: [...candidateKey, candidatePage, candidateSize],
    queryFn: async ({ signal }) => {
      owner.assert(signal);
      const result = await api.candidates(
        itemId,
        { limit: candidateSize, offset: candidatePage * candidateSize },
        signal,
      );
      owner.assert(signal);
      return result;
    },
    enabled: open && adding && !readOnly && owner.enabled,
    placeholderData: keepPreviousData,
  });
  useEffect(() => {
    if (
      replicas.data &&
      !replicas.isPlaceholderData &&
      page > 0 &&
      page * pageSize >= replicas.data.total
    )
      setPage(Math.max(0, Math.ceil(replicas.data.total / pageSize) - 1));
  }, [replicas.data, replicas.isPlaceholderData, page, pageSize]);
  useEffect(() => {
    if (
      candidates.data &&
      !candidates.isPlaceholderData &&
      candidatePage > 0 &&
      candidatePage * candidateSize >= candidates.data.total
    )
      setCandidatePage(Math.max(0, Math.ceil(candidates.data.total / candidateSize) - 1));
  }, [candidates.data, candidates.isPlaceholderData, candidatePage, candidateSize]);
  const act = async (operation: (signal: AbortSignal) => Promise<unknown>, success: string) => {
    if (readOnly || pending.current) return;
    const current = owner.capture();
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      owner.assert(controller.signal);
      await operation(controller.signal);
      owner.assert(controller.signal);
      if (!current()) return;
      setMessage(success);
      await Promise.all([
        client.invalidateQueries({ queryKey: listKey }),
        client.invalidateQueries({ queryKey: candidateKey }),
      ]);
      if (!current() || controller.signal.aborted) return;
      await onChanged?.();
    } catch (failure) {
      if (current() && !controller.signal.aborted) {
        setError(errorMessage(failure));
        if (
          failure instanceof ApiError &&
          failure.detailRaw &&
          typeof failure.detailRaw === "object" &&
          (failure.detailRaw as { code?: unknown }).code === "dataset_archived"
        )
          setServerArchived(true);
      }
    } finally {
      if (pending.current === controller) pending.current = null;
      if (current() && !controller.signal.aborted) setBusy(false);
    }
  };
  const disabled = busy || replicas.isPlaceholderData || replicas.isFetching;
  const priority = newPriority.trim() === "" ? NaN : Number(newPriority);
  const validPriority = Number.isInteger(priority) && priority >= 0 && priority <= 1000;
  return (
    <details
      className="mt-4 border-t border-border pt-3 text-sm"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer text-sm font-semibold focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
        备用来源{replicas.data ? `（${replicas.data.total}）` : ""}
      </summary>
      <div className="mt-3 space-y-3">
        <p id={hint} className="text-xs leading-relaxed text-muted-foreground">
          没有可用的完整缓存时，按优先级尝试原图来源。当前来源默认 100，数字越小越优先。
          关联只增加读取来源，不会合并样本或标注；解除关联不会删除原图或样本。
        </p>
        {readOnly && (
          <p className="text-xs text-muted-foreground">数据集已归档，备用来源保持只读。</p>
        )}
        {error && (
          <p role="alert" className="text-xs leading-relaxed text-status-danger">
            {error}
          </p>
        )}
        {message && (
          <p role="status" className="text-xs text-muted-foreground">
            {message}
          </p>
        )}
        {replicas.isPending && (
          <p role="status" className="text-xs text-muted-foreground">
            正在读取备用来源…
          </p>
        )}
        {replicas.error && (
          <div className="space-y-2">
            <p role="alert" className="text-xs text-status-danger">
              {errorMessage(replicas.error)}
            </p>
            <Button
              size="sm"
              disabled={busy || replicas.isFetching}
              onClick={() => void replicas.refetch()}
            >
              重试读取备用来源
            </Button>
          </div>
        )}
        {replicas.data && (
          <>
            {replicas.data.items.length ? (
              <ul
                aria-label="已关联备用来源"
                aria-busy={replicas.isFetching || busy}
                className="divide-y divide-border"
              >
                {replicas.data.items.map((replica) => (
                  <ReplicaRow
                    key={`${replica.id}/${replica.priority}`}
                    replica={replica}
                    archived={readOnly}
                    busy={disabled}
                    onUpdate={(value) =>
                      void act(
                        (signal) => api.update(itemId, replica.id, value, signal),
                        "备用来源优先级已更新。",
                      )
                    }
                    onRemove={() =>
                      void act(
                        (signal) => api.remove(itemId, replica.id, signal),
                        "已解除备用来源关联。",
                      )
                    }
                  />
                ))}
              </ul>
            ) : (
              <p className="text-xs text-muted-foreground">尚未关联备用来源。</p>
            )}
            <ListPagination
              label="备用来源"
              page={page}
              pageSize={pageSize}
              total={replicas.data.total}
              loading={busy || replicas.isFetching}
              onPageChange={setPage}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPage(0);
              }}
            />
          </>
        )}
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            disabled={busy || replicas.isFetching || candidates.isFetching}
            onClick={() => {
              setError("");
              void replicas.refetch();
              if (adding && !readOnly) void candidates.refetch();
            }}
          >
            刷新列表
          </Button>
          {!readOnly && (
            <Button
              size="sm"
              disabled={busy}
              aria-expanded={adding}
              onClick={() => setAdding((value) => !value)}
            >
              {adding ? "收起候选来源" : "关联备用来源"}
            </Button>
          )}
        </div>
        {!readOnly && adding && (
          <section aria-label="候选备用来源" className="space-y-3 border-t border-border pt-3">
            <p className="text-xs leading-relaxed text-muted-foreground">
              候选图片与当前样本的内容记录一致，读取时仍会核对完整图片。
            </p>
            <label className="grid w-36 gap-1 text-xs text-muted-foreground">
              新增来源优先级
              <input
                className={field}
                type="number"
                min={0}
                max={1000}
                step={1}
                value={newPriority}
                aria-describedby={`${hint} ${hint}-priority`}
                disabled={busy}
                onChange={(event) => setNewPriority(event.target.value)}
              />
            </label>
            <p id={`${hint}-priority`} className="text-xs leading-relaxed text-muted-foreground">
              填写 0–1000 的整数，默认 100。
            </p>
            {candidates.isPending && (
              <p role="status" className="text-xs text-muted-foreground">
                正在查找候选来源…
              </p>
            )}
            {candidates.error && (
              <div className="space-y-2">
                <p role="alert" className="text-xs text-status-danger">
                  {errorMessage(candidates.error)}
                </p>
                <Button
                  size="sm"
                  disabled={busy || candidates.isFetching}
                  onClick={() => void candidates.refetch()}
                >
                  重试查找候选来源
                </Button>
              </div>
            )}
            {candidates.data && (
              <>
                {candidates.data.items.length ? (
                  <ul
                    aria-label="可关联来源"
                    aria-busy={candidates.isFetching || busy}
                    className="divide-y divide-border"
                  >
                    {candidates.data.items.map((candidate) => (
                      <li
                        key={candidate.item_id}
                        className="flex flex-wrap items-center justify-between gap-3 py-3"
                      >
                        <SourceInfo source={candidate} />
                        <Button
                          size="sm"
                          disabled={
                            busy ||
                            !validPriority ||
                            candidates.isPlaceholderData ||
                            candidates.isFetching
                          }
                          aria-label={`关联${candidate.source_name}`}
                          onClick={() =>
                            void act(
                              (signal) => api.add(itemId, candidate.item_id, priority, signal),
                              "备用来源已关联，样本保持独立。",
                            )
                          }
                        >
                          关联
                        </Button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-xs text-muted-foreground">暂无可关联的其他来源。</p>
                )}
                <ListPagination
                  label="候选来源"
                  page={candidatePage}
                  pageSize={candidateSize}
                  total={candidates.data.total}
                  loading={busy || candidates.isFetching}
                  onPageChange={setCandidatePage}
                  onPageSizeChange={(size) => {
                    setCandidateSize(size);
                    setCandidatePage(0);
                  }}
                />
              </>
            )}
          </section>
        )}
      </div>
    </details>
  );
}
