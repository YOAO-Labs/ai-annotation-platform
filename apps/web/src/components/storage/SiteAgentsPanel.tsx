import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import { siteAgentsApi } from "@/api/siteAgents";
import type {
  AgentLimits,
  AgentOut,
  AgentPairingOut,
  AgentRoot,
  SiteOut,
} from "@/api/generated/types.gen";
import { useAuthStore } from "@/stores/authStore";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { copyToClipboard } from "@/lib/clipboard";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { Modal } from "@/components/ui/Modal";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { StorageLocationAccessFields } from "@/components/connections/StorageLocationAccessFields";
import { StorageCacheFields } from "@/components/connections/StorageCacheFields";
import { storageAccessDenied } from "@/components/connections/storageQueryState";
import { safeDatasetReturn } from "@/components/datasets/storageNavigation";
import { originalCacheValues, type CachePolicy } from "@/utils/cachePolicy";

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const field =
  "w-full min-w-0 rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const hint = "text-xs leading-relaxed text-muted-foreground";
const STATES = {
  pending: ["待配对", "outline"],
  online: ["在线", "success"],
  degraded: ["降级", "warning"],
  offline: ["离线", "default"],
  revoked: ["已撤销", "danger"],
} as const;
type Dialog =
  | { kind: "site" }
  | { kind: "agent"; siteId?: string }
  | { kind: "pair"; pairing: AgentPairingOut }
  | { kind: "register"; root: AgentRoot }
  | { kind: "rotate" | "revoke"; agent: AgentOut };

function pageIndex(value: string | null) {
  const n = Number(value ?? 1);
  return Number.isSafeInteger(n) && n > 0 && n <= 1_000_000 ? n - 1 : 0;
}
function date(value: string | null) {
  return value ? new Date(value).toLocaleString() : "尚未报告";
}
function errorMessage(error: unknown) {
  const detail =
    typeof error === "object" && error && "detailRaw" in error ? error.detailRaw : null;
  const code = typeof detail === "object" && detail && "code" in detail ? String(detail.code) : "";
  const messages: Record<string, string> = {
    site_name_conflict: "该站点名称已存在，请使用其他名称。",
    site_not_found: "站点不存在或已不可访问，请刷新站点列表。",
    agent_not_found: "Agent 不存在或已不可访问。",
    agent_revoked: "Agent 已撤销，请重新添加 Agent。",
    agent_unpaired: "Agent 尚未完成配对，请先在站点服务器完成配对。",
    credential_expired: "Agent 凭证已过期，请暂停派发后轮换身份并重新配对。",
    credential_revision_changed: "身份修订已变化，请刷新详情后重试。",
    agent_tasks_not_drained: "请先暂停派发并等待活动任务结束，再轮换身份。",
    agent_root_revision_changed: "受控根修订已变化，请返回详情刷新后重新选择。",
    invalid_relative_path: "请填写受控根下的相对目录，不允许绝对路径或上级目录。",
  };
  if (messages[code]) return messages[code];
  if (detail === "Storage location name already exists")
    return "该存储位置名称已存在，请使用其他名称。";
  if (typeof error === "object" && error && "status" in error && error.status === 409)
    return "Agent 状态、身份修订或活动任务已变化。请刷新详情后重试；轮换前须暂停派发并等待任务结束。";
  return verifiedContentError(error);
}
function AgentState({ agent }: { agent: AgentOut }) {
  const [label, variant] = STATES[agent.state];
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Badge variant={variant} dot>
        {label}
      </Badge>
      {agent.draining && agent.state !== "revoked" && <Badge variant="warning">已暂停派发</Badge>}
    </span>
  );
}
function Limits({ value }: { value: AgentLimits | null }) {
  if (!value) return <p className={hint}>尚未报告运行预算。</p>;
  return (
    <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-3">
      <div>
        <dt className={hint}>并发上限</dt>
        <dd>{value.max_concurrency ?? "—"}</dd>
      </div>
      <div>
        <dt className={hint}>带宽上限</dt>
        <dd>
          {value.bandwidth_bytes_per_second == null
            ? "—"
            : `${value.bandwidth_bytes_per_second / 1024} KiB/s`}
        </dd>
      </div>
      <div>
        <dt className={hint}>单文件上限</dt>
        <dd>{value.max_bytes == null ? "—" : `${value.max_bytes / 1024 ** 2} MiB`}</dd>
      </div>
    </dl>
  );
}

export function SiteAgentsPanel() {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  if (role !== "super_admin")
    return <p className="text-sm text-muted-foreground">站点与 Agent 仅超级管理员可查看。</p>;
  return <SiteAgentsOwner key={`${revision}/${role}`} />;
}

function SiteAgentsOwner() {
  const owner = useAuthStore((s) => `${s.user?.id}/${s.sessionId}/${s.user?.role}`);
  const capture = useSessionGuard();
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const siteId = UUID.test(params.get("agent_site") ?? "") ? params.get("agent_site")! : "";
  const agentId = UUID.test(params.get("agent") ?? "") ? params.get("agent")! : "";
  const page = pageIndex(params.get("agents_page"));
  const pageSize = listPageSize(params.get("agents_page_size"));
  const [locationPage, setLocationPage] = useState(0);
  const [locationPageSize, setLocationPageSize] = useState(20);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [createdSite, setCreatedSite] = useState<SiteOut | null>(null);
  const epoch = useRef(0);
  const trigger = useRef<HTMLElement | null>(null);
  const initialSetupShown = useRef(false);
  const siteHint = useId();
  const patch = (values: Record<string, string | null>) =>
    setParams((current) => {
      const next = new URLSearchParams(current);
      Object.entries(values).forEach(([key, value]) =>
        value === null ? next.delete(key) : next.set(key, value),
      );
      return next;
    });
  const sites = useInfiniteQuery({
    queryKey: ["site-agent-sites", owner],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => siteAgentsApi.sites(pageParam, signal),
    getNextPageParam: (last, pages) => {
      const loaded = pages.reduce((n, p) => n + p.items.length, 0);
      return loaded < last.total && last.items.length > 0 ? loaded : undefined;
    },
    retry: false,
  });
  const agents = useQuery({
    queryKey: ["site-agents", owner, siteId, page * pageSize, pageSize],
    queryFn: ({ signal }) => siteAgentsApi.agents(page * pageSize, pageSize, siteId, signal),
    refetchInterval: 30_000,
    retry: false,
  });
  const detail = useQuery({
    queryKey: ["site-agent-detail", owner, agentId],
    queryFn: ({ signal }) => siteAgentsApi.agent(agentId, signal),
    enabled: !!agentId,
    refetchInterval: 30_000,
    retry: false,
  });
  const locations = useQuery({
    queryKey: ["site-agent-locations", owner, agentId, locationPage, locationPageSize],
    queryFn: ({ signal }) =>
      siteAgentsApi.locations(agentId, locationPage * locationPageSize, locationPageSize, signal),
    enabled: !!agentId && !!detail.data && !storageAccessDenied(detail.error),
    retry: false,
  });
  const siteData = storageAccessDenied(sites.error) ? undefined : sites.data;
  const agentData = storageAccessDenied(agents.error) ? undefined : agents.data;
  const agent = storageAccessDenied(detail.error) ? undefined : detail.data;
  const locationData = storageAccessDenied(locations.error) ? undefined : locations.data;
  const siteOptions = [
    ...new Map(
      [
        ...(siteData?.pages.flatMap((p) => p.items) ?? []),
        ...(siteData && createdSite ? [createdSite] : []),
      ].map((site) => [site.id, site]),
    ).values(),
  ];
  const siteName = (id: string) => siteOptions.find((site) => site.id === id)?.name ?? id;
  const invalidate = () => {
    for (const key of [
      "site-agent-sites",
      "site-agents",
      "site-agent-detail",
      "site-agent-locations",
      "storage-source-locations",
    ])
      void client.invalidateQueries({ queryKey: [key, owner] });
  };
  const changeDialog = (next: Dialog | null) => {
    epoch.current += 1;
    setDialog(next);
    setBusy(false);
    setError("");
  };
  const close = () => {
    changeDialog(null);
    patch({ agent: null });
  };
  useEffect(() => {
    epoch.current += 1;
    setDialog(null);
    setBusy(false);
    setError("");
    setLocationPage(0);
  }, [agentId]);
  useEffect(() => {
    if (siteData?.pages[0]?.total === 0 && !agentId && !initialSetupShown.current) {
      initialSetupShown.current = true;
      setDialog({ kind: "site" });
    }
  }, [siteData, agentId]);
  useEffect(() => {
    if (!agentData || agents.isFetching) return;
    const last = Math.max(0, Math.ceil(agentData.total / pageSize) - 1);
    if (page > last)
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          next.set("agents_page", String(last + 1));
          return next;
        },
        { replace: true },
      );
  }, [agentData, agents.isFetching, page, pageSize, setParams]);
  useEffect(() => {
    if (!locationData || locations.isFetching) return;
    const last = Math.max(0, Math.ceil(locationData.total / locationPageSize) - 1);
    if (locationPage > last) setLocationPage(last);
  }, [locationData, locations.isFetching, locationPage, locationPageSize]);
  // Secret-bearing responses belong to the current dialog as well as the current session.
  const act = async <T,>(request: () => Promise<T>, done: (result: T) => void) => {
    const current = capture();
    const expected = epoch.current;
    setBusy(true);
    setError("");
    try {
      const result = await request();
      if (!current()) return;
      invalidate();
      if (expected === epoch.current) done(result);
    } catch (cause) {
      if (current() && expected === epoch.current) setError(errorMessage(cause));
    } finally {
      if (current() && expected === epoch.current) setBusy(false);
    }
  };
  const open = (next: Dialog, element: HTMLElement) => {
    trigger.current = element;
    changeDialog(next);
  };
  const detailKey = ["site-agent-detail", owner, agentId];
  const returnTo = safeDatasetReturn(params.get("returnTo"));
  const locationLink = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("tab", "locations");
    next.set("location", id);
    next.delete("agent");
    if (!returnTo) next.delete("returnTo");
    return `/storage?${next}`;
  };
  const modalTitle =
    dialog?.kind === "site"
      ? "新建站点"
      : dialog?.kind === "agent"
        ? "添加 Agent"
        : dialog?.kind === "pair"
          ? "Agent 配对信息"
          : dialog?.kind === "register"
            ? "登记 Agent 存储位置"
            : dialog?.kind === "rotate"
              ? "轮换 Agent 身份"
              : dialog?.kind === "revoke"
                ? "撤销 Agent"
                : "Agent 详情";

  return (
    <>
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border p-4">
          <div className="min-w-0">
            <h2 className="text-md font-semibold">站点与 Agent</h2>
            <p className={`mt-1 ${hint}`}>管理站点连接，再将 Agent 报告的受控根登记为存储位置。</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button onClick={(e) => open({ kind: "site" }, e.currentTarget)}>新建站点</Button>
            <Button
              variant="primary"
              disabled={!siteOptions.length}
              onClick={(e) => open({ kind: "agent", siteId }, e.currentTarget)}
            >
              添加 Agent
            </Button>
          </div>
        </div>
        <div className="grid gap-2 border-b border-border p-4 sm:max-w-md">
          <label className="grid gap-1 text-sm">
            站点筛选
            <select
              className={field}
              value={siteId}
              aria-describedby={siteHint}
              onChange={(e) => patch({ agent_site: e.target.value || null, agents_page: null })}
            >
              <option value="">全部站点</option>
              {siteId && !siteOptions.some((site) => site.id === siteId) && (
                <option value={siteId}>{siteId}</option>
              )}
              {siteOptions.map((site) => (
                <option key={site.id} value={site.id}>
                  {site.name}
                </option>
              ))}
            </select>
          </label>
          <p id={siteHint} className={hint}>
            站点按页加载；未找到目标时可继续加载站点。
          </p>
          {sites.hasNextPage && (
            <Button
              size="sm"
              disabled={sites.isFetchingNextPage}
              onClick={() => void sites.fetchNextPage()}
            >
              加载更多站点
            </Button>
          )}
          {sites.isPending && (
            <p role="status" className={hint}>
              正在加载站点…
            </p>
          )}
          {sites.error && (
            <p role="alert" className="text-xs text-status-danger">
              无法读取站点。{errorMessage(sites.error)}
            </p>
          )}
        </div>
        {agents.isPending ? (
          <p role="status" className="p-4 text-sm text-muted-foreground">
            正在加载 Agent…
          </p>
        ) : agents.error && !agentData ? (
          <p role="alert" className="p-4 text-sm text-status-danger">
            无法读取 Agent。{errorMessage(agents.error)}
          </p>
        ) : (
          <>
            {agents.error && (
              <p role="alert" className="p-4 text-xs text-status-danger">
                刷新失败，保留上次结果。{errorMessage(agents.error)}
              </p>
            )}
            {!agentData?.items.length ? (
              <p className="p-4 text-sm text-muted-foreground">
                {agentData?.total
                  ? "当前页没有 Agent，请返回上一页。"
                  : "尚无 Agent。添加后在站点服务器完成配对。"}
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {agentData.items.map((item) => (
                  <li
                    key={item.id}
                    className="flex flex-wrap items-center justify-between gap-3 p-4"
                  >
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2 text-sm">
                        <span className="break-all font-medium">{item.name}</span>
                        <AgentState agent={item} />
                      </div>
                      <p className={`mt-1 break-all ${hint}`}>
                        {siteName(item.site_id)} · {item.roots.length} 个受控根 · 最后心跳{" "}
                        {date(item.last_seen)}
                      </p>
                    </div>
                    <Button
                      size="sm"
                      aria-label={`查看 ${item.name}`}
                      onClick={(e) => {
                        trigger.current = e.currentTarget;
                        patch({ agent: item.id });
                      }}
                    >
                      查看详情
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
        <ListPagination
          label="Agent"
          page={page}
          pageSize={pageSize}
          total={agentData?.total}
          loading={agents.isFetching}
          onPageChange={(n) => patch({ agents_page: String(n + 1) })}
          onPageSizeChange={(n) => patch({ agents_page_size: String(n), agents_page: null })}
        />
      </Card>
      <Modal
        open={!!agentId || !!dialog}
        onClose={close}
        title={modalTitle}
        width={800}
        returnFocusRef={trigger}
      >
        <div className="min-w-0 space-y-4 text-sm" aria-busy={busy}>
          {error && (
            <p role="alert" className="break-words text-status-danger">
              {error}
            </p>
          )}
          {dialog?.kind === "site" ? (
            <SiteForm
              busy={busy}
              submit={(name, region) =>
                void act(
                  () => siteAgentsApi.createSite({ name, region: region || null }),
                  (site) => {
                    setCreatedSite(site);
                    changeDialog({ kind: "agent", siteId: site.id });
                  },
                )
              }
            />
          ) : dialog?.kind === "agent" ? (
            <AgentForm
              sites={siteOptions}
              initialSite={dialog.siteId}
              hasMore={sites.hasNextPage}
              loadingMore={sites.isFetchingNextPage}
              loadMore={() => void sites.fetchNextPage()}
              busy={busy}
              submit={(id, name, limits) =>
                void act(
                  () => siteAgentsApi.createAgent(id, { name, limits }),
                  (pairing) => changeDialog({ kind: "pair", pairing }),
                )
              }
            />
          ) : dialog?.kind === "pair" ? (
            <PairingInfo
              pairing={dialog.pairing}
              showAgent={() => {
                const id = dialog.pairing.agent_id;
                changeDialog(null);
                patch({ agent: id });
              }}
            />
          ) : dialog?.kind === "register" && agent ? (
            <LocationForm
              agent={agent}
              root={dialog.root}
              owner={owner}
              busy={busy}
              submit={(payload) =>
                void act(
                  () => siteAgentsApi.registerLocation(payload),
                  (location) => {
                    close();
                    patch({ tab: "locations", location: location.id, agent: null });
                  },
                )
              }
              back={() => changeDialog(null)}
            />
          ) : dialog?.kind === "rotate" || dialog?.kind === "revoke" ? (
            <>
              <p className="break-words">
                {dialog.kind === "rotate"
                  ? `轮换“${dialog.agent.name}”的身份后，旧凭证失效，须用新的配对码重新连接。Agent 必须已暂停派发，且活动任务已结束。`
                  : `撤销“${dialog.agent.name}”后将拒绝该 Agent 的连接和读取。关联位置与来源会保留，需管理员另行处理；此操作无法恢复原凭证。`}
              </p>
              <p className={hint}>本次确认针对身份修订 {dialog.agent.credential_revision}。</p>
              <div className="flex flex-wrap gap-2">
                <Button disabled={busy} onClick={() => changeDialog(null)}>
                  取消
                </Button>
                <Button
                  variant="danger"
                  disabled={busy}
                  onClick={() => {
                    if (dialog.kind === "rotate")
                      void act(
                        () =>
                          siteAgentsApi.rotate(dialog.agent.id, dialog.agent.credential_revision),
                        (pairing) => changeDialog({ kind: "pair", pairing }),
                      );
                    else
                      void act(
                        () =>
                          siteAgentsApi.revoke(dialog.agent.id, dialog.agent.credential_revision),
                        (result) => {
                          client.setQueryData(detailKey, result);
                          changeDialog(null);
                        },
                      );
                  }}
                >
                  {dialog.kind === "rotate" ? "确认轮换身份" : "确认撤销 Agent"}
                </Button>
              </div>
            </>
          ) : detail.isPending ? (
            <p role="status" className={hint}>
              正在加载 Agent 详情…
            </p>
          ) : !agent ? (
            <p role="alert" className="text-status-danger">
              无法读取 Agent 详情。{errorMessage(detail.error)}
            </p>
          ) : (
            <>
              {detail.error && (
                <p role="alert" className="text-status-danger">
                  刷新失败，保留上次详情。{errorMessage(detail.error)}
                </p>
              )}
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="break-all text-md font-semibold">{agent.name}</h3>
                  <p className={`mt-1 break-all ${hint}`}>{siteName(agent.site_id)}</p>
                </div>
                <AgentState agent={agent} />
              </div>
              <dl className="grid min-w-0 gap-3 sm:grid-cols-2">
                <div>
                  <dt className={hint}>Agent ID</dt>
                  <dd className="break-all">{agent.id}</dd>
                </div>
                <div>
                  <dt className={hint}>最后心跳</dt>
                  <dd>{date(agent.last_seen)}</dd>
                </div>
                <div>
                  <dt className={hint}>协议 / 身份修订</dt>
                  <dd>
                    {agent.protocol_version} / {agent.credential_revision}
                  </dd>
                </div>
                <div>
                  <dt className={hint}>配对码到期时间</dt>
                  <dd>{agent.pair_expires_at ? date(agent.pair_expires_at) : "无待用配对码"}</dd>
                </div>
              </dl>
              <div className="flex flex-wrap gap-2">
                <Button
                  disabled={detail.isFetching}
                  onClick={() => {
                    void detail.refetch();
                    void locations.refetch();
                  }}
                >
                  刷新详情
                </Button>
                {agent.state !== "revoked" && (
                  <>
                    <Button
                      disabled={busy}
                      onClick={() =>
                        void act(
                          () =>
                            siteAgentsApi.drain(
                              agent.id,
                              agent.credential_revision,
                              !agent.draining,
                            ),
                          (result) => client.setQueryData(detailKey, result),
                        )
                      }
                    >
                      {agent.draining ? "恢复派发" : "暂停派发"}
                    </Button>
                    <Button
                      disabled={busy || !agent.draining}
                      onClick={() => changeDialog({ kind: "rotate", agent })}
                    >
                      轮换身份
                    </Button>
                    <Button
                      variant="danger"
                      disabled={busy}
                      onClick={() => changeDialog({ kind: "revoke", agent })}
                    >
                      撤销 Agent
                    </Button>
                  </>
                )}
              </div>
              {agent.state !== "revoked" && (
                <p className={hint}>
                  暂停派发后不再领取新任务；已有任务可完成。轮换身份须等待活动任务结束。
                </p>
              )}
              <section className="space-y-2 border-t border-border pt-4">
                <h3 className="text-sm font-semibold">受控根</h3>
                {!agent.roots.length ? (
                  <p className={hint}>
                    {agent.state === "pending"
                      ? "等待站点服务器完成配对并报告受控根。"
                      : "尚未报告受控根，请启动 Agent 并等待心跳。"}
                  </p>
                ) : (
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {agent.roots.map((root) => (
                      <li
                        key={root.root_handle}
                        className="flex flex-wrap items-center justify-between gap-2 p-3"
                      >
                        <div className="min-w-0">
                          <span className="break-all font-medium">{root.root_handle}</span>
                          <p className={hint}>根修订 {root.root_revision}</p>
                        </div>
                        <Button
                          size="sm"
                          disabled={agent.state === "revoked"}
                          onClick={() => changeDialog({ kind: "register", root })}
                        >
                          登记存储位置
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
              <section className="space-y-2 border-t border-border pt-4">
                <h3 className="text-sm font-semibold">关联存储位置</h3>
                {locations.isPending ? (
                  <p role="status" className={hint}>
                    正在加载位置…
                  </p>
                ) : locations.error && !locationData ? (
                  <p role="alert" className="text-status-danger">
                    无法读取关联位置。{errorMessage(locations.error)}
                  </p>
                ) : (
                  <>
                    {locations.error && (
                      <p role="alert" className="text-status-danger">
                        位置刷新失败，保留上次结果。
                      </p>
                    )}
                    {!locationData?.items.length ? (
                      <p className={hint}>
                        {locationData?.total
                          ? "当前页没有位置，请返回上一页。"
                          : "尚未登记存储位置。"}
                      </p>
                    ) : (
                      <ul className="divide-y divide-border">
                        {locationData.items.map((location) => (
                          <li
                            key={location.id}
                            className="flex flex-wrap items-center justify-between gap-2 py-2"
                          >
                            <div className="min-w-0">
                              <Link
                                className="break-all text-brand underline"
                                to={locationLink(location.id)}
                              >
                                {location.name}
                              </Link>
                              <p className={hint}>
                                {location.source_usage
                                  ? `${location.source_usage.reference_count} 个已登记引用`
                                  : "引用规模待加载"}{" "}
                                · {location.enabled ? "已启用" : "已停用"}
                              </p>
                            </div>
                            <span className={`${hint} break-all`}>
                              {location.root_handle} / {location.relative_path || "根目录"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </>
                )}
                <ListPagination
                  label="Agent 关联位置"
                  page={locationPage}
                  pageSize={locationPageSize}
                  total={locationData?.total}
                  loading={locations.isFetching}
                  onPageChange={setLocationPage}
                  onPageSizeChange={(size) => {
                    setLocationPageSize(size);
                    setLocationPage(0);
                  }}
                />
              </section>
              <details className="rounded-md border border-border p-3">
                <summary className="cursor-pointer font-medium">资源预算与连接信息</summary>
                <div className="mt-3 space-y-3">
                  <h4 className="text-xs font-medium text-muted-foreground">平台预算</h4>
                  <Limits value={agent.limits} />
                  <h4 className="text-xs font-medium text-muted-foreground">Agent 报告预算</h4>
                  <Limits value={agent.reported_limits} />
                  <p className={`break-all ${hint}`}>
                    当前会话：{agent.session_id ?? "尚未连接"} · 创建于 {date(agent.created_at)}
                  </p>
                </div>
              </details>
            </>
          )}
        </div>
      </Modal>
    </>
  );
}

function FormActions({ busy, children }: { busy: boolean; children: ReactNode }) {
  return (
    <div className="flex flex-wrap gap-2 pt-2">
      <Button type="submit" variant="primary" disabled={busy}>
        {busy ? "正在提交…" : children}
      </Button>
    </div>
  );
}
function SiteForm({
  busy,
  submit,
}: {
  busy: boolean;
  submit: (name: string, region: string) => void;
}) {
  const [name, setName] = useState("");
  const [region, setRegion] = useState("");
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim()) submit(name.trim(), region.trim());
      }}
    >
      <label className="grid gap-1">
        站点名称
        <input
          className={field}
          autoFocus
          required
          maxLength={100}
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="grid gap-1">
        区域（可选）
        <input
          className={field}
          maxLength={100}
          value={region}
          disabled={busy}
          onChange={(e) => setRegion(e.target.value)}
        />
      </label>
      <FormActions busy={busy}>创建站点并添加 Agent</FormActions>
    </form>
  );
}
function AgentForm({
  sites,
  initialSite,
  hasMore,
  loadingMore,
  loadMore,
  busy,
  submit,
}: {
  sites: SiteOut[];
  initialSite?: string;
  hasMore: boolean;
  loadingMore: boolean;
  loadMore: () => void;
  busy: boolean;
  submit: (siteId: string, name: string, limits: AgentLimits) => void;
}) {
  const [siteId, setSiteId] = useState(initialSite || sites[0]?.id || "");
  const [name, setName] = useState("");
  const [concurrency, setConcurrency] = useState("1");
  const [bandwidth, setBandwidth] = useState("8192");
  const [bytes, setBytes] = useState("64");
  const siteHint = useId();
  const submitForm = (e: FormEvent) => {
    e.preventDefault();
    if (siteId && name.trim())
      submit(siteId, name.trim(), {
        max_concurrency: Number(concurrency),
        bandwidth_bytes_per_second: Number(bandwidth) * 1024,
        max_bytes: Number(bytes) * 1024 ** 2,
      });
  };
  return (
    <form className="space-y-4" onSubmit={submitForm}>
      <label className="grid gap-1">
        所属站点
        <select
          className={field}
          required
          value={siteId}
          disabled={busy}
          aria-describedby={siteHint}
          onChange={(e) => setSiteId(e.target.value)}
        >
          <option value="" disabled hidden />
          {siteId && !sites.some((site) => site.id === siteId) && (
            <option value={siteId}>{siteId}</option>
          )}
          {sites.map((site) => (
            <option key={site.id} value={site.id}>
              {site.name}
            </option>
          ))}
        </select>
        <span id={siteHint} className={hint}>
          在该站点的服务器运行 Agent；一个站点可添加多个 Agent。
        </span>
      </label>
      {hasMore && (
        <Button type="button" size="sm" disabled={busy || loadingMore} onClick={loadMore}>
          加载更多站点
        </Button>
      )}
      <label className="grid gap-1">
        Agent 名称
        <input
          className={field}
          autoFocus
          required
          maxLength={100}
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer font-medium">资源预算</summary>
        <div className="mt-3 grid gap-3 sm:grid-cols-3">
          <label className="grid gap-1">
            并发上限
            <input
              className={field}
              type="number"
              min={1}
              max={4}
              step={1}
              required
              disabled={busy}
              value={concurrency}
              onChange={(e) => setConcurrency(e.target.value)}
            />
          </label>
          <label className="grid gap-1">
            带宽（KiB/s）
            <input
              className={field}
              type="number"
              min={1}
              max={1048576}
              step={1}
              required
              disabled={busy}
              value={bandwidth}
              onChange={(e) => setBandwidth(e.target.value)}
            />
          </label>
          <label className="grid gap-1">
            单文件（MiB）
            <input
              className={field}
              type="number"
              min={1}
              max={64}
              step={1}
              required
              disabled={busy}
              value={bytes}
              onChange={(e) => setBytes(e.target.value)}
            />
          </label>
        </div>
      </details>
      <FormActions busy={busy}>创建并获取配对码</FormActions>
    </form>
  );
}
function PairingInfo({ pairing, showAgent }: { pairing: AgentPairingOut; showAgent: () => void }) {
  const capture = useSessionGuard();
  const [copied, setCopied] = useState<string>("");
  return (
    <div className="space-y-3">
      <p>配对码仅在本次弹窗展示，关闭后无法再次查看。请在到期前交给站点 Agent 完成配对。</p>
      <dl className="space-y-3">
        <div>
          <dt className={hint}>Agent ID</dt>
          <dd className="break-all">{pairing.agent_id}</dd>
        </div>
        <div>
          <dt className={hint}>配对码</dt>
          <dd>
            <code className="block break-all rounded-md border border-border bg-muted p-3 text-sm">
              {pairing.pair_code}
            </code>
          </dd>
        </div>
        <div>
          <dt className={hint}>到期时间</dt>
          <dd>{date(pairing.expires_at)}</dd>
        </div>
      </dl>
      <p className={hint}>
        复制结果为包含 agent_id 和 pair_code 的 JSON。将其保存为仅运行账户可读的配对文件，再交由
        Agent 配对命令读取。
      </p>
      <div className="flex flex-wrap gap-2">
        <Button
          onClick={async () => {
            const current = capture();
            const ok = await copyToClipboard(
              JSON.stringify({ agent_id: pairing.agent_id, pair_code: pairing.pair_code }, null, 2),
            );
            if (current()) setCopied(ok ? "已复制配对信息" : "复制失败，请手动选择配对码");
          }}
        >
          复制配对信息
        </Button>
        <Button onClick={showAgent}>查看 Agent</Button>
      </div>
      {copied && (
        <p role="status" className={hint}>
          {copied}
        </p>
      )}
    </div>
  );
}
function LocationForm({
  agent,
  root,
  owner,
  busy,
  submit,
  back,
}: {
  agent: AgentOut;
  root: AgentRoot;
  owner: string;
  busy: boolean;
  submit: (payload: Parameters<typeof siteAgentsApi.registerLocation>[0]) => void;
  back: () => void;
}) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [scope, setScope] = useState<"restricted" | "global">("restricted");
  const [users, setUsers] = useState<string[]>([]);
  const [policy, setPolicy] = useState<CachePolicy>("preview");
  const [quota, setQuota] = useState("0");
  const [ttl, setTTL] = useState("86400");
  const cache = originalCacheValues(policy, quota, ttl);
  const pathHint = useId();
  return (
    <form
      className="space-y-4"
      onSubmit={(e) => {
        e.preventDefault();
        if (!cache.valid || !name.trim()) return;
        submit({
          name: name.trim(),
          agent_id: agent.id,
          root_handle: root.root_handle,
          root_revision: root.root_revision,
          relative_path: path.trim(),
          scope,
          use_user_ids: scope === "restricted" ? users : [],
          cache_policy: policy,
          original_cache_quota_bytes: cache.original_cache_quota_bytes,
          original_cache_ttl_seconds: cache.original_cache_ttl_seconds,
        });
      }}
    >
      <p className="break-all">
        {agent.name} · {root.root_handle} · 根修订 {root.root_revision}
      </p>
      <label className="grid gap-1">
        位置名称
        <input
          className={field}
          autoFocus
          required
          maxLength={100}
          value={name}
          disabled={busy}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <label className="grid gap-1">
        根下相对目录
        <input
          aria-label="根下相对目录"
          className={field}
          value={path}
          disabled={busy}
          aria-describedby={pathHint}
          onChange={(e) => setPath(e.target.value)}
        />
        <span id={pathHint} className={hint}>
          留空表示整个受控根；例如 traffic/vehicle。只登记 Agent
          已报告根下的目录，不填写服务器绝对路径。
        </span>
      </label>
      <details className="rounded-md border border-border p-3">
        <summary className="cursor-pointer font-medium">使用权限与缓存</summary>
        <StorageLocationAccessFields
          session={owner}
          scope={scope}
          userIds={users}
          disabled={busy}
          onScope={setScope}
          onUsers={setUsers}
        />
        <div className="mt-4">
          <StorageCacheFields
            policy={policy}
            quotaMiB={quota}
            ttl={ttl}
            disabled={busy}
            onPolicy={setPolicy}
            onQuota={setQuota}
            onTTL={setTTL}
          />
        </div>
      </details>
      {!cache.valid && (
        <p role="alert" className="text-xs text-status-danger">
          请检查缓存额度和保留时间。
        </p>
      )}
      <p className={hint}>登记只保存位置配置；实际可读性由 Agent 心跳与后续探测确认。</p>
      <div className="flex flex-wrap gap-2">
        <Button type="button" disabled={busy} onClick={back}>
          返回详情
        </Button>
        <Button type="submit" variant="primary" disabled={busy || !cache.valid}>
          {busy ? "正在登记…" : "登记并查看位置"}
        </Button>
      </div>
    </form>
  );
}
