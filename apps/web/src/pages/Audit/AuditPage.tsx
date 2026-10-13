import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { FilterTrigger } from "@/components/filters/FilterTrigger";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { Input } from "@/components/shadcn/ui/input";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentProps,
  type CSSProperties,
  type ReactNode,
} from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useAuditLogs, useAuditMonthlySummary } from "@/hooks/useAudit";
import { useUsers } from "@/hooks/useUsers";
import { auditApi } from "@/api/audit";
import { AUDIT_BUSINESS_ACTIONS, AUDIT_TARGET_TYPES, auditActionLabel } from "@/utils/auditLabels";
import { roleLabel } from "@/constants/roles";
import type { AuditLogResponse, AuditSummaryBucket } from "@/api/audit";
import { useElementStyle } from "@/components/ui/useElementStyle";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { AUDIT_URL_DEFAULTS, AUDIT_URL_KEYS, auditUrlCodec } from "./auditUrlState";
import styles from "./AuditPage.module.css";

const PAGE_SIZE = 20;

function cssVar(name: string): string {
  if (typeof window === "undefined") return "";
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function AuditPage() {
  const urlState = useUrlFilterState({
    codec: auditUrlCodec,
    defaults: AUDIT_URL_DEFAULTS,
    ownedKeys: AUDIT_URL_KEYS,
  });
  const { state: filters, issues, patch } = urlState;
  const [targetIdDraft, setTargetIdDraft] = useState(filters.targetId);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [actorQuery, setActorQuery] = useState("");
  const [detailKeyDraft, setDetailKeyDraft] = useState(filters.detailKey);
  const [detailValueDraft, setDetailValueDraft] = useState(filters.detailValue);
  const syncingTextDrafts = useRef({ targetId: false, detailKey: false, detailValue: false });
  const lastUrlText = useRef({
    targetId: filters.targetId,
    detailKey: filters.detailKey,
    detailValue: filters.detailValue,
  });
  const debouncedTargetId = useDebouncedValue(targetIdDraft, 250);
  const debouncedDetailKey = useDebouncedValue(detailKeyDraft, 250);
  const debouncedDetailValue = useDebouncedValue(detailValueDraft, 250);
  const page = filters.page;
  const scope = filters.scope;
  const actionFilter = filters.action;
  const targetType = filters.targetType;
  const targetId = filters.targetId;
  const actorId = filters.actorId;
  const detailKey = filters.detailKey;
  const detailValue = filters.detailValue;
  const [detail, setDetail] = useState<AuditLogResponse | null>(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [exporting, setExporting] = useState(false);
  const pushToast = useToastStore((s) => s.push);

  const { data: usersData = [] } = useUsers();

  useEffect(() => {
    const previous = lastUrlText.current;
    if (
      previous.targetId === filters.targetId &&
      previous.detailKey === filters.detailKey &&
      previous.detailValue === filters.detailValue
    ) {
      return;
    }
    lastUrlText.current = {
      targetId: filters.targetId,
      detailKey: filters.detailKey,
      detailValue: filters.detailValue,
    };
    const syncTargetId =
      previous.targetId !== filters.targetId && targetIdDraft !== filters.targetId;
    const syncDetailKey =
      previous.detailKey !== filters.detailKey && detailKeyDraft !== filters.detailKey;
    const syncDetailValue =
      previous.detailValue !== filters.detailValue && detailValueDraft !== filters.detailValue;
    // Rehydrating one field must not suspend a pending edit in another field.
    if (previous.targetId !== filters.targetId) {
      syncingTextDrafts.current.targetId = syncTargetId;
      if (syncTargetId) setTargetIdDraft(filters.targetId);
    }
    if (previous.detailKey !== filters.detailKey) {
      syncingTextDrafts.current.detailKey = syncDetailKey;
      if (syncDetailKey) setDetailKeyDraft(filters.detailKey);
    }
    if (previous.detailValue !== filters.detailValue) {
      syncingTextDrafts.current.detailValue = syncDetailValue;
      if (syncDetailValue) setDetailValueDraft(filters.detailValue);
    }
  }, [
    detailKeyDraft,
    detailValueDraft,
    filters.detailKey,
    filters.detailValue,
    filters.targetId,
    targetIdDraft,
  ]);

  useEffect(() => {
    if (syncingTextDrafts.current.targetId) {
      if (debouncedTargetId === filters.targetId) {
        syncingTextDrafts.current.targetId = false;
      }
      return;
    }
    const nextTargetId = debouncedTargetId.trim();
    if (nextTargetId !== targetIdDraft.trim()) return;
    if (nextTargetId === filters.targetId) return;
    patch({ targetId: nextTargetId, page: 1 }, { replace: true });
  }, [debouncedTargetId, filters.targetId, patch, targetIdDraft]);

  useEffect(() => {
    if (syncingTextDrafts.current.detailKey) {
      if (debouncedDetailKey === filters.detailKey) syncingTextDrafts.current.detailKey = false;
      return;
    }
    const nextDetailKey = debouncedDetailKey.trim();
    if (nextDetailKey !== detailKeyDraft.trim()) return;
    if (nextDetailKey === filters.detailKey) return;
    patch({ detailKey: nextDetailKey, page: 1 }, { replace: true });
  }, [debouncedDetailKey, detailKeyDraft, filters.detailKey, patch]);

  useEffect(() => {
    if (syncingTextDrafts.current.detailValue) {
      if (debouncedDetailValue === filters.detailValue)
        syncingTextDrafts.current.detailValue = false;
      return;
    }
    if (debouncedDetailValue !== detailValueDraft) return;
    if (debouncedDetailValue === filters.detailValue) return;
    patch({ detailValue: debouncedDetailValue, page: 1 }, { replace: true });
  }, [debouncedDetailValue, detailValueDraft, filters.detailValue, patch]);

  const focused = !!actorId || !!targetId || !!targetType || !!actionFilter || !!detailKey;
  const focusedActor = actorId ? usersData.find((u) => u.id === actorId) : null;

  const clearFocus = () => {
    patch(
      {
        action: "",
        targetType: "",
        targetId: "",
        actorId: "",
        detailKey: "",
        detailValue: "",
        page: 1,
      },
      { replace: false },
    );
  };

  const drillDownAction = (action: string) => {
    patch({ action, page: 1 }, { replace: false });
  };

  const appliedFilters = useMemo(
    () => ({
      action: actionFilter || undefined,
      target_type: targetType || undefined,
      target_id: targetId || undefined,
      actor_id: actorId || undefined,
      detail_key: detailKey || undefined,
      detail_value: detailKey ? detailValue : undefined,
      business_only: scope === "business" ? true : undefined,
    }),
    [actionFilter, actorId, detailKey, detailValue, scope, targetId, targetType],
  );
  const params = useMemo(
    () => ({ ...appliedFilters, page, page_size: PAGE_SIZE }),
    [appliedFilters, page],
  );
  const { data, isLoading, refetch, isFetching } = useAuditLogs(params, {
    refetchInterval: autoRefresh ? 30_000 : false,
  });

  const handleExport = async (format: "csv" | "json") => {
    if (exporting) return;
    setExporting(true);
    try {
      await auditApi.export(appliedFilters, format);
      pushToast({ msg: `已导出审计日志 ${format.toUpperCase()}`, kind: "success" });
    } catch (err) {
      pushToast({
        msg: "导出失败",
        sub: err instanceof Error ? err.message : String(err),
        kind: "error",
      });
    } finally {
      setExporting(false);
    }
  };

  const items = useMemo(() => data?.items ?? [], [data?.items]);

  const total = data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // v0.6.6 · 按 request_id 分组：同一 HTTP 请求的 metadata 行 + 业务 detail 行折叠为单行 + ▸ 展开
  // v0.7.0：折叠状态 sessionStorage 持久化（30min TTL），刷新页面后自动恢复最近展开的 request_id。
  const [expandedReqIds, setExpandedReqIds] = useState<Set<string>>(() => {
    if (typeof window === "undefined") return new Set();
    try {
      const raw = sessionStorage.getItem("audit:expanded");
      if (!raw) return new Set();
      const parsed = JSON.parse(raw) as { ts: number; ids: string[] };
      if (Date.now() - parsed.ts > 30 * 60 * 1000) {
        sessionStorage.removeItem("audit:expanded");
        return new Set();
      }
      return new Set(parsed.ids);
    } catch {
      return new Set();
    }
  });

  useEffect(() => {
    try {
      sessionStorage.setItem(
        "audit:expanded",
        JSON.stringify({ ts: Date.now(), ids: Array.from(expandedReqIds) }),
      );
    } catch {
      // ignore quota
    }
  }, [expandedReqIds]);
  type Group = { id: string; leader: AuditLogResponse; children: AuditLogResponse[] };
  const groups: Group[] = useMemo(() => {
    const buckets = new Map<string, AuditLogResponse[]>();
    const ordered: string[] = [];
    items.forEach((it) => {
      const key = it.request_id || `__solo_${it.id}`;
      if (!buckets.has(key)) {
        buckets.set(key, []);
        ordered.push(key);
      }
      buckets.get(key)!.push(it);
    });
    return ordered.map((key) => {
      const rows = buckets.get(key)!;
      // 选 leader：优先非 http.* 的业务行；否则用 http.* 元数据行
      const business = rows.find((r) => !r.action.startsWith("http."));
      const leader = business ?? rows[0];
      const children = rows.filter((r) => r.id !== leader.id);
      return { id: key, leader, children };
    });
  }, [items]);

  // 平铺成 virtualizable rows：[group-leader, ...expanded-children]*
  type FlatRow =
    | { kind: "leader"; group: Group }
    | { kind: "child"; group: Group; row: AuditLogResponse };
  const flatRows: FlatRow[] = useMemo(() => {
    const out: FlatRow[] = [];
    groups.forEach((g) => {
      out.push({ kind: "leader", group: g });
      if (expandedReqIds.has(g.id)) {
        g.children.forEach((row) => out.push({ kind: "child", group: g, row }));
      }
    });
    return out;
  }, [groups, expandedReqIds]);

  const tableContainerRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: flatRows.length,
    getScrollElement: () => tableContainerRef.current,
    estimateSize: () => 44,
    overscan: 8,
  });

  const toggleGroup = (id: string) => {
    setExpandedReqIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className={styles.page}>
      <header className={styles.header}>
        <div>
          <h1 className={styles.title}>审计日志</h1>
          <p className={styles.description}>
            所有写操作（POST/PATCH/PUT/DELETE）由中间件捕获；关键业务事件携带结构化 detail。
          </p>
          {!!issues.length && (
            <div role="alert" className="mt-1 text-xs text-status-caution">
              URL 审计筛选无法完整恢复，已使用安全默认值。
            </div>
          )}
        </div>
        <div className={styles.actions}>
          <label className={styles.autoRefresh}>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className={styles.checkbox}
            />
            30s 自动刷新
          </label>
          <Button onClick={() => handleExport("csv")} disabled={exporting}>
            <Icon name="download" size={12} />
            CSV
          </Button>
          <Button onClick={() => handleExport("json")} disabled={exporting}>
            <Icon name="download" size={12} />
            JSON
          </Button>
          <Button onClick={() => refetch()}>
            <Icon name="refresh" size={12} />
            刷新
          </Button>
        </div>
      </header>

      <Card>
        <div className={styles.filters}>
          {/* 标签自解释（选项即范围），hideLabel 避免纯装饰文案把下拉挤到第二行导致面板错位。 */}
          <FilterGroup label="事件范围" hideLabel compact>
            <FilterSelect
              aria-label="审计事件范围"
              value={scope}
              onChange={(e) => {
                patch({ scope: e.target.value as "business" | "all", page: 1 }, { replace: false });
              }}
            >
              <option value="business">仅业务事件</option>
              <option value="all">全部（含 HTTP 元数据）</option>
            </FilterSelect>
          </FilterGroup>
          <FilterPanel
            open={filtersOpen}
            onOpenChange={setFiltersOpen}
            trigger={
              <FilterTrigger
                count={
                  [actionFilter, targetType, actorId, targetId, detailKey].filter(Boolean).length
                }
              />
            }
            title="审计筛选"
            description="即时生效 · 详情键值允许为空字符串。"
            align="start"
            footer={
              <div className="flex justify-between gap-2">
                <Button size="sm" variant="ghost" onClick={clearFocus}>
                  清除追溯
                </Button>
                <Button size="sm" onClick={() => setFiltersOpen(false)}>
                  完成
                </Button>
              </div>
            }
          >
            <div className="grid gap-3">
              <label className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground">
                动作
                <FilterSelect
                  aria-label="审计动作"
                  value={actionFilter}
                  onChange={(e) => {
                    patch({ action: e.target.value, page: 1 }, { replace: false });
                  }}
                  className="w-full"
                >
                  <option value="">全部动作</option>
                  {AUDIT_BUSINESS_ACTIONS.map((a) => (
                    <option key={a} value={a}>
                      {auditActionLabel(a)}
                    </option>
                  ))}
                </FilterSelect>
              </label>
              <label className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground">
                对象类型
                <FilterSelect
                  aria-label="审计对象类型"
                  value={targetType}
                  onChange={(e) => {
                    patch({ targetType: e.target.value, page: 1 }, { replace: false });
                  }}
                  className="w-full"
                >
                  <option value="">全部对象</option>
                  {AUDIT_TARGET_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </FilterSelect>
              </label>
              <div className="space-y-2">
                <span className="text-xs font-medium text-muted-foreground">操作人</span>
                <Input
                  aria-label="搜索审计操作人"
                  placeholder="搜索姓名或邮箱"
                  value={actorQuery}
                  onChange={(event) => setActorQuery(event.target.value)}
                  className="h-8"
                />
                <FilterSelect
                  aria-label="审计操作人"
                  value={actorId}
                  onChange={(e) => {
                    patch({ actorId: e.target.value, page: 1 }, { replace: false });
                  }}
                  className="w-full"
                >
                  <option value="">全部用户</option>
                  {actorId && !usersData.some((user) => user.id === actorId) && (
                    <option value={actorId}>当前指定用户</option>
                  )}
                  {usersData
                    .filter(
                      (user) =>
                        user.id === actorId ||
                        `${user.name} ${user.email}`
                          .toLocaleLowerCase()
                          .includes(actorQuery.trim().toLocaleLowerCase()),
                    )
                    .map((u) => (
                      <option key={u.id} value={u.id}>
                        {u.name} · {u.email}
                      </option>
                    ))}
                </FilterSelect>
              </div>
              <label className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground">
                对象 ID
                <Input
                  aria-label="对象 ID（精确匹配）"
                  value={targetIdDraft}
                  placeholder="对象 ID（精确匹配）"
                  onChange={(e) => {
                    syncingTextDrafts.current.targetId = false;
                    setTargetIdDraft(e.target.value);
                  }}
                  className="h-8"
                />
              </label>
              <label className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground">
                详情键名
                <Input
                  aria-label="detail 键名"
                  value={detailKeyDraft}
                  placeholder="detail 键名（如 role）"
                  title="A.3：detail_json 字段级 GIN 过滤——键名"
                  onChange={(e) => {
                    syncingTextDrafts.current.detailKey = false;
                    setDetailKeyDraft(e.target.value);
                  }}
                  className="h-8"
                />
              </label>
              <label className="flex min-w-0 flex-col gap-1.5 text-xs text-muted-foreground">
                详情键值
                <Input
                  aria-label="detail 键值"
                  value={detailValueDraft}
                  placeholder="detail 键值（如 super_admin）"
                  title="A.3：detail_json 字段级 GIN 过滤——键值（与键名共同生效）"
                  onChange={(e) => {
                    syncingTextDrafts.current.detailValue = false;
                    setDetailValueDraft(e.target.value);
                  }}
                  disabled={!detailKey}
                  className="h-8"
                />
              </label>
            </div>
          </FilterPanel>
          <span className={styles.totalText}>
            共 {total} 条 · 第 {page} / {pageCount} 页
          </span>
        </div>

        <AuditMonthlySummaryPanel
          businessOnly={scope === "business"}
          onActionDrillDown={drillDownAction}
        />

        {focused && (
          <div className={styles.focusBar}>
            <FilterGroup label="追溯模式" hideLabel compact className={styles.focusTags}>
              {actorId && (
                <ActiveFilterChip
                  label="操作人"
                  value={focusedActor ? `${focusedActor.name} · ${focusedActor.email}` : actorId}
                  onClick={() => setFiltersOpen(true)}
                  onRemove={() => patch({ actorId: "", page: 1 }, { replace: false })}
                />
              )}
              {targetType && (
                <ActiveFilterChip
                  label="对象类型"
                  value={targetType}
                  onClick={() => setFiltersOpen(true)}
                  onRemove={() => patch({ targetType: "", page: 1 }, { replace: false })}
                />
              )}
              {targetId && (
                <ActiveFilterChip
                  label="对象 ID"
                  value={targetId}
                  onClick={() => setFiltersOpen(true)}
                  onRemove={() => patch({ targetId: "", page: 1 }, { replace: false })}
                />
              )}
              {actionFilter && (
                <ActiveFilterChip
                  label="动作"
                  value={actionFilter}
                  onClick={() => setFiltersOpen(true)}
                  onRemove={() => patch({ action: "", page: 1 }, { replace: false })}
                />
              )}
              {detailKey && (
                <ActiveFilterChip
                  label={`detail.${detailKey}`}
                  value={detailValue === "" ? "（空字符串）" : detailValue}
                  onClick={() => setFiltersOpen(true)}
                  onRemove={() =>
                    patch({ detailKey: "", detailValue: "", page: 1 }, { replace: false })
                  }
                />
              )}
            </FilterGroup>
            <Button size="sm" variant="ghost" onClick={clearFocus}>
              <Icon name="x" size={11} />
              清除追溯
            </Button>
          </div>
        )}

        {/* v0.6.6 · 按 request_id 折叠为单行 + ▸ 展开；virtualized 容器 */}
        <div className={styles.tableHeader}>
          {["", "时间", "操作人", "动作", "对象", "IP", "状态", ""].map((h, i) => (
            <div key={i} className={styles.headerCell}>
              {h}
            </div>
          ))}
        </div>

        <div ref={tableContainerRef} className={styles.tableViewport}>
          {(isLoading || isFetching) && flatRows.length === 0 && (
            <div className={styles.emptyState}>加载中...</div>
          )}
          {!isLoading && flatRows.length === 0 && <div className={styles.emptyState}>暂无记录</div>}
          <VirtualSizer height={virtualizer.getTotalSize()}>
            {virtualizer.getVirtualItems().map((virt) => {
              const row = flatRows[virt.index];
              const expanded = row.kind === "leader" && expandedReqIds.has(row.group.id);
              const it = row.kind === "leader" ? row.group.leader : row.row;
              const isLeader = row.kind === "leader";
              const hasChildren = isLeader && row.group.children.length > 0;
              return (
                <VirtualAuditRow
                  key={virt.key}
                  index={virt.index}
                  start={virt.start}
                  isChild={row.kind === "child"}
                  measureElement={virtualizer.measureElement}
                >
                  <div className={styles.expandCell}>
                    {hasChildren ? (
                      <button
                        type="button"
                        onClick={() => toggleGroup(row.group.id)}
                        title={expanded ? "折叠" : `展开同请求 ${row.group.children.length + 1} 条`}
                        className={styles.focusButton}
                      >
                        <Icon name={expanded ? "chevDown" : "chevRight"} size={12} />
                        <span className={styles.childCount}>{row.group.children.length + 1}</span>
                      </button>
                    ) : null}
                  </div>
                  <div className={styles.timeCell}>
                    {new Date(it.created_at).toLocaleString("zh-CN", { hour12: false })}
                  </div>
                  <div className={styles.cell}>
                    {it.actor_email ? (
                      <div className={styles.actorWrap}>
                        {it.actor_id ? (
                          <button
                            type="button"
                            onClick={() => {
                              patch({ actorId: it.actor_id!, page: 1 }, { replace: false });
                            }}
                            title="按操作人追溯"
                            className={styles.focusButton}
                          >
                            {it.actor_email}
                          </button>
                        ) : (
                          <span className={styles.actorEmail}>{it.actor_email}</span>
                        )}
                        {it.actor_role && (
                          <span className={styles.tinyBadge}>
                            <Badge variant="outline">{roleLabel(it.actor_role)}</Badge>
                          </span>
                        )}
                      </div>
                    ) : (
                      <span className={styles.anonymous}>匿名</span>
                    )}
                  </div>
                  <div className={styles.cell}>
                    <SmallBadge variant={it.action.startsWith("http.") ? "outline" : "accent"}>
                      {auditActionLabel(it.action)}
                    </SmallBadge>
                  </div>
                  <div className={styles.targetCell}>
                    {it.target_type && it.target_id ? (
                      <button
                        type="button"
                        onClick={() => {
                          patch(
                            {
                              targetType: it.target_type!,
                              targetId: it.target_id!,
                              page: 1,
                            },
                            { replace: false },
                          );
                        }}
                        title={`按对象 ${it.target_type}/${it.target_id} 追溯`}
                        className={styles.focusButton}
                      >
                        {it.target_type}
                        <span className={`mono ${styles.targetId}`}>
                          {it.target_id.length > 24 ? it.target_id.slice(0, 8) + "…" : it.target_id}
                        </span>
                      </button>
                    ) : it.target_type ? (
                      <span>{it.target_type}</span>
                    ) : (
                      "—"
                    )}
                  </div>
                  <div className={styles.ipCell}>{it.ip ?? "—"}</div>
                  <div className={styles.cell}>{statusBadge(it.status_code)}</div>
                  <div className={styles.detailCell}>
                    <Button size="sm" variant="ghost" onClick={() => setDetail(it)}>
                      详情
                    </Button>
                  </div>
                </VirtualAuditRow>
              );
            })}
          </VirtualSizer>
        </div>

        <div className={styles.pagination}>
          <Button
            size="sm"
            disabled={page <= 1}
            onClick={() => patch({ page: Math.max(1, page - 1) }, { replace: false })}
          >
            <Icon name="chevLeft" size={11} />
            上一页
          </Button>
          <Button
            size="sm"
            disabled={page >= pageCount}
            onClick={() => patch({ page: page + 1 }, { replace: false })}
          >
            下一页
            <Icon name="chevRight" size={11} />
          </Button>
        </div>
      </Card>

      <Modal open={!!detail} onClose={() => setDetail(null)} title="审计日志详情" width={620}>
        {detail && (
          <div className={styles.detailStack}>
            <div className={styles.detailActions}>
              {detail.actor_id && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    patch(
                      {
                        actorId: detail.actor_id!,
                        targetType: "",
                        targetId: "",
                        action: "",
                        page: 1,
                      },
                      { replace: false },
                    );
                    setDetail(null);
                  }}
                >
                  <Icon name="activity" size={11} /> 该操作人完整时间线
                </Button>
              )}
              {detail.target_type && detail.target_id && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    patch(
                      {
                        targetType: detail.target_type!,
                        targetId: detail.target_id!,
                        actorId: "",
                        action: "",
                        page: 1,
                      },
                      { replace: false },
                    );
                    setDetail(null);
                  }}
                >
                  <Icon name="activity" size={11} /> 该对象完整时间线
                </Button>
              )}
            </div>
            <KV label="时间" value={new Date(detail.created_at).toLocaleString("zh-CN")} />
            <KV label="操作人" value={detail.actor_email ?? "匿名"} mono />
            <KV label="动作" value={`${auditActionLabel(detail.action)} (${detail.action})`} />
            <KV
              label="对象"
              value={`${detail.target_type ?? "-"} / ${detail.target_id ?? "-"}`}
              mono
            />
            <KV label="HTTP" value={`${detail.method ?? "-"} ${detail.path ?? "-"}`} mono />
            <KV label="状态" value={String(detail.status_code ?? "-")} />
            <KV label="IP" value={detail.ip ?? "-"} mono />
            <div>
              <div className={styles.detailJsonLabel}>detail_json</div>
              <pre className={styles.detailJson}>
                {detail.detail_json
                  ? JSON.stringify(detail.detail_json, null, 2)
                  : "(空 — 中间件元数据行)"}
              </pre>
            </div>
          </div>
        )}
      </Modal>
    </div>
  );
}

function AuditMonthlySummaryPanel({
  businessOnly,
  onActionDrillDown,
}: {
  businessOnly: boolean;
  onActionDrillDown: (action: string) => void;
}) {
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7));
  const summaryQ = useAuditMonthlySummary(month, businessOnly);
  const data = summaryQ.data;

  const trendData = useMemo(
    () =>
      (data?.daily ?? []).map((point) => ({
        day: point.day.slice(8),
        事件: point.event_count,
        错误: point.error_count,
      })),
    [data?.daily],
  );
  const actionData = useMemo(
    () =>
      (data?.top_actions ?? []).map((item) => ({
        action: item.key,
        label: auditActionLabel(item.key).slice(0, 18),
        event_count: item.event_count,
      })),
    [data?.top_actions],
  );

  const brand = cssVar("--sc-brand");
  const danger = cssVar("--sc-destructive");
  const muted = cssVar("--sc-muted-foreground");
  const grid = cssVar("--sc-border");
  const tick = useMemo(() => ({ fill: muted, fontSize: 11 }), [muted]);
  const tooltipStyle = useMemo<CSSProperties>(
    () => ({
      background: "var(--sc-card)",
      border: "1px solid var(--sc-border)",
      borderRadius: "var(--radius-md)",
      color: "var(--sc-foreground)",
    }),
    [],
  );

  const handleChartClick = (state: unknown) => {
    const action = (state as { activePayload?: Array<{ payload?: { action?: string } }> } | null)
      ?.activePayload?.[0]?.payload?.action;
    if (action) onActionDrillDown(action);
  };

  return (
    <section className={styles.summarySection} aria-labelledby="audit-summary-title">
      <div className={styles.summaryHeader}>
        <div>
          <div className={styles.summaryTitleRow}>
            <h2 id="audit-summary-title" className={styles.summaryTitle}>
              月度概览
            </h2>
            <span className={styles.utcBadge}>UTC</span>
          </div>
          <p className={styles.summaryHint}>
            {data?.materialized_through
              ? `历史聚合至 ${data.materialized_through}，后续日期实时补齐`
              : "物化尚未覆盖，所选月份由在线审计分区实时计算"}
          </p>
        </div>
        <label className={styles.monthPicker}>
          <span>月份</span>
          <input
            type="month"
            value={month}
            onChange={(event) => setMonth(event.target.value)}
            className={styles.control}
            aria-label="审计概览月份"
          />
        </label>
      </div>

      {summaryQ.isLoading && <div className={styles.summaryState}>加载月度概览...</div>}
      {summaryQ.isError && (
        <div className={styles.summaryState} role="alert">
          <span>无法加载月度概览，审计明细仍可继续使用。</span>
          <Button size="sm" variant="ghost" onClick={() => summaryQ.refetch()}>
            重试
          </Button>
        </div>
      )}

      {data && (
        <>
          <dl className={styles.kpiStrip}>
            <SummaryKpi label="事件总数" value={data.totals.event_count} />
            <SummaryKpi label="错误事件" value={data.totals.error_count} tone="danger" />
            <SummaryKpi label="动作类型" value={data.totals.action_kind_count} />
          </dl>

          {data.totals.event_count === 0 ? (
            <div className={styles.summaryState}>所选月份暂无审计事件</div>
          ) : (
            <>
              <div className={styles.summaryCharts}>
                <section className={styles.chartPanel} aria-labelledby="audit-trend-title">
                  <div className={styles.chartHeading}>
                    <h3 id="audit-trend-title">每日事件趋势</h3>
                    <span>错误包含 4xx 与 5xx</span>
                  </div>
                  <div className={styles.chartCanvas}>
                    <ResponsiveContainer width="100%" height={230}>
                      <LineChart
                        data={trendData}
                        margin={{ top: 12, right: 12, bottom: 4, left: -12 }}
                      >
                        <CartesianGrid stroke={grid} strokeDasharray="3 3" vertical={false} />
                        <XAxis
                          dataKey="day"
                          tick={tick}
                          axisLine={{ stroke: grid }}
                          tickLine={false}
                        />
                        <YAxis
                          allowDecimals={false}
                          tick={tick}
                          axisLine={false}
                          tickLine={false}
                        />
                        <Tooltip contentStyle={tooltipStyle} />
                        <Line
                          type="linear"
                          dataKey="事件"
                          stroke={brand}
                          strokeWidth={2}
                          dot={false}
                          activeDot={{ r: 3 }}
                        />
                        <Line
                          type="linear"
                          dataKey="错误"
                          stroke={danger}
                          strokeWidth={1.5}
                          dot={false}
                          activeDot={{ r: 3 }}
                        />
                      </LineChart>
                    </ResponsiveContainer>
                  </div>
                </section>

                <section className={styles.chartPanel} aria-labelledby="audit-actions-title">
                  <div className={styles.chartHeading}>
                    <h3 id="audit-actions-title">Top actions</h3>
                    <span>点击条形或下方动作进入明细</span>
                  </div>
                  <div className={`${styles.chartCanvas} ${styles.actionChart}`}>
                    <ResponsiveContainer width="100%" height={230}>
                      <BarChart
                        layout="vertical"
                        data={actionData}
                        margin={{ top: 4, right: 16, bottom: 4, left: 8 }}
                        onClick={handleChartClick}
                      >
                        <CartesianGrid stroke={grid} strokeDasharray="3 3" horizontal={false} />
                        <XAxis
                          type="number"
                          allowDecimals={false}
                          tick={tick}
                          axisLine={{ stroke: grid }}
                          tickLine={false}
                        />
                        <YAxis
                          type="category"
                          dataKey="label"
                          width={118}
                          tick={tick}
                          axisLine={false}
                          tickLine={false}
                        />
                        <Tooltip contentStyle={tooltipStyle} />
                        <Bar
                          dataKey="event_count"
                          name="事件数"
                          fill={brand}
                          radius={[0, 3, 3, 0]}
                        />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                  <div className={styles.actionLinks} aria-label="Top action 明细下钻">
                    {data.top_actions.map((item) => (
                      <button
                        key={item.key}
                        type="button"
                        className={styles.actionLink}
                        onClick={() => onActionDrillDown(item.key)}
                      >
                        <span>{auditActionLabel(item.key)}</span>
                        <strong>{item.event_count.toLocaleString("zh-CN")}</strong>
                      </button>
                    ))}
                  </div>
                </section>
              </div>

              <div className={styles.dimensionGrid}>
                <SummaryRanking title="目标类型" items={data.target_types} />
                <SummaryRanking title="操作人角色" items={data.actor_roles} roleLabels />
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}

function SummaryKpi({ label, value, tone }: { label: string; value: number; tone?: "danger" }) {
  return (
    <div className={styles.kpiItem}>
      <dt>{label}</dt>
      <dd className={tone === "danger" ? styles.kpiDanger : undefined}>
        {value.toLocaleString("zh-CN")}
      </dd>
    </div>
  );
}

function SummaryRanking({
  title,
  items,
  roleLabels = false,
}: {
  title: string;
  items: AuditSummaryBucket[];
  roleLabels?: boolean;
}) {
  return (
    <section className={styles.rankingPanel}>
      <h3>{title}</h3>
      <div className={styles.rankingRows}>
        {items.map((item) => (
          <div key={item.key || "__system"} className={styles.rankingRow}>
            <span>{item.key ? (roleLabels ? roleLabel(item.key) : item.key) : "系统/未知"}</span>
            <strong>{item.event_count.toLocaleString("zh-CN")}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function statusBadge(code: number | null) {
  if (code === null) return <span className={styles.subtleText}>—</span>;
  if (code >= 500) return <SmallBadge variant="danger">{code}</SmallBadge>;
  if (code >= 400) return <SmallBadge variant="warning">{code}</SmallBadge>;
  if (code >= 200) return <SmallBadge variant="success">{code}</SmallBadge>;
  return <SmallBadge variant="outline">{code}</SmallBadge>;
}

function KV({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className={styles.kvRow}>
      <div className={styles.kvLabel}>{label}</div>
      <div className={mono ? `mono ${styles.kvValue}` : styles.kvValue}>{value}</div>
    </div>
  );
}

function SmallBadge({
  children,
  variant = "accent",
}: {
  children: ReactNode;
  variant?: ComponentProps<typeof Badge>["variant"];
}) {
  return (
    <Badge variant={variant} className={styles.smallBadge}>
      {children}
    </Badge>
  );
}

function VirtualSizer({ height, children }: { height: number; children: ReactNode }) {
  const styleRef = useElementStyle<HTMLDivElement>(
    useMemo<CSSProperties>(() => ({ height }), [height]),
  );
  return (
    <div ref={styleRef} className={styles.virtualSizer}>
      {children}
    </div>
  );
}

function VirtualAuditRow({
  index,
  start,
  isChild,
  measureElement,
  children,
}: {
  index: number;
  start: number;
  isChild: boolean;
  measureElement: (element: Element | null) => void;
  children: ReactNode;
}) {
  const rowStyle = useMemo<CSSProperties>(
    () => ({ "--audit-row-y": `${start}px` }) as CSSProperties,
    [start],
  );
  const styleRef = useElementStyle<HTMLDivElement>(rowStyle);
  const setRowRef = useCallback(
    (node: HTMLDivElement | null) => {
      styleRef(node);
      measureElement(node);
    },
    [measureElement, styleRef],
  );

  return (
    <div
      ref={setRowRef}
      data-index={index}
      className={isChild ? `${styles.auditRow} ${styles.auditRowChild}` : styles.auditRow}
    >
      {children}
    </div>
  );
}
