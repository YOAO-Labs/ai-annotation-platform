import { FilterSelect } from "@/components/filters/FilterControls";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { FilterTrigger } from "@/components/filters/FilterTrigger";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
/**
 * v0.23.4 P3 · registry "问题中心" tab (Super Admin only).
 *
 * Plan §4.3 / §5（模型市场多 TAB UI 优化 · 阶段一）: same
 * `code + subject_type + subject_id` renders only ONE primary record; affected
 * objects are listed under it with readable names and can jump to their
 * registry sub-view via the object focus keys (`pool_id` / `instance_id` /
 * `gpu_id`), which stay separate from the issue filter keys. When the source
 * data has no matching record the summary stays non-interactive — no disabled
 * pseudo-links.
 *
 * Filters are URL-owned: `issue_q` (code text search, independent of the exact
 * `issue_code` filter), `issue_severity` quick filter and the attached
 * pool/instance/gpu/code conditions. Filter options prefer readable object
 * names, appending short ids only on name collisions; the applied value is
 * always the stable id. 清除条件 keeps the text search.
 */
import { useMemo, useState, type ReactNode } from "react";

import { Input } from "@/components/shadcn/ui/input";

import { formatShortId } from "./registryShared";
import { EmptyState, UrlIssueChips } from "./registryUi";
import type { RegistrySectionProps } from "./registryTypes";
import type { Diagnostic, DiagnosticFilter } from "../runtimeTopology";
import { filterDiagnostics, sortDiagnostics } from "../runtimeTopology";
import { focusPatchFor, urlIssuesForView } from "../marketUrlState";
import { SEVERITY_TOKENS } from "../runtime/StateTokens";

export function IssueCenter({ scope, url, patchUrl, urlIssues }: RegistrySectionProps): ReactNode {
  const { diagnostics } = scope;
  const [filtersOpen, setFiltersOpen] = useState(false);

  const filter = useMemo<DiagnosticFilter>(
    () => ({
      pool_id: url.issuePool || undefined,
      instance_id: url.issueInstance || undefined,
      gpu_resource_id: url.issueGpu || undefined,
      code: url.issueCode || undefined,
    }),
    [url.issuePool, url.issueInstance, url.issueGpu, url.issueCode],
  );
  const activeFilterCount = [
    filter.pool_id,
    filter.instance_id,
    filter.gpu_resource_id,
    filter.code,
  ].filter(Boolean).length;

  const sorted = useMemo(() => sortDiagnostics(diagnostics), [diagnostics]);
  const filtered = useMemo(() => {
    const withCode = url.issueQ.trim()
      ? sorted.filter((d) => d.code.toLowerCase().includes(url.issueQ.trim().toLowerCase()))
      : sorted;
    const withConditions = filterDiagnostics(withCode, filter);
    return url.issueSeverity === "all"
      ? withConditions
      : withConditions.filter((d) => d.severity === url.issueSeverity);
  }, [sorted, filter, url.issueQ, url.issueSeverity]);

  // Distinct codes + subjects for filter dropdowns.
  const codes = useMemo(() => Array.from(new Set(sorted.map((d) => d.code))).sort(), [sorted]);
  const poolIds = useMemo(
    () => Array.from(new Set(sorted.flatMap((d) => d.affected_service_pool_ids))).sort(),
    [sorted],
  );
  const instanceIds = useMemo(
    () => Array.from(new Set(sorted.flatMap((d) => d.affected_instance_ids))).sort(),
    [sorted],
  );
  const gpuIds = useMemo(
    () => Array.from(new Set(sorted.flatMap((d) => d.affected_gpu_resource_ids))).sort(),
    [sorted],
  );

  const counts = useMemo(() => {
    const by: Record<Diagnostic["severity"], number> = {
      blocker: 0,
      critical: 0,
      warning: 0,
      info: 0,
    };
    for (const d of sorted) by[d.severity] += 1;
    return by;
  }, [sorted]);

  const clearConditions = () =>
    patchUrl({
      issueSeverity: "all",
      issuePool: "",
      issueInstance: "",
      issueGpu: "",
      issueCode: "",
    });

  return (
    <div className="flex flex-col gap-3">
      {/* Severity summary */}
      <div className="flex flex-wrap items-center gap-2">
        <SummaryBadge label="阻断" count={counts.blocker} variant="danger" />
        <SummaryBadge label="严重" count={counts.critical} variant="danger" />
        <SummaryBadge label="告警" count={counts.warning} variant="warning" />
        <SummaryBadge label="提示" count={counts.info} variant="accent" />
        <span className="text-2xs text-muted-foreground">
          共 {sorted.length} 条（已按 code+subject 去重）
        </span>
      </div>

      {/* Filter bar — belongs to this view only (plan §4.3). */}
      <div className="flex flex-wrap items-center gap-2">
        <Input
          aria-label="按 code 搜索"
          placeholder="按 code 搜索（如 circuit_open）"
          value={url.issueQ}
          onChange={(e) => patchUrl({ issueQ: e.target.value })}
          className="h-8 w-56 text-xs"
        />
        <FilterSelect
          aria-label="按严重度筛选"
          value={url.issueSeverity}
          onChange={(e) => patchUrl({ issueSeverity: e.target.value as typeof url.issueSeverity })}
          className="h-8 text-xs"
        >
          <option value="all">全部严重度</option>
          <option value="blocker">阻断</option>
          <option value="critical">严重</option>
          <option value="warning">告警</option>
          <option value="info">提示</option>
        </FilterSelect>
        <FilterPanel
          open={filtersOpen}
          onOpenChange={setFiltersOpen}
          trigger={<FilterTrigger count={activeFilterCount} />}
          title="筛选诊断"
          description="即时生效 · 按服务池、实例、GPU 和 code 过滤。"
          align="start"
          footer={
            <div className="flex justify-end">
              <Button size="sm" onClick={() => setFiltersOpen(false)}>
                完成
              </Button>
            </div>
          }
        >
          <div className="grid gap-3">
            <DiagnosticFilterSelect
              label="服务池"
              value={url.issuePool}
              options={poolIds}
              resolveLabel={(id) => disambiguatedName("pool", id, poolIds, scope)}
              onChange={(v) => patchUrl({ issuePool: v })}
            />
            <DiagnosticFilterSelect
              label="实例"
              value={url.issueInstance}
              options={instanceIds}
              resolveLabel={(id) => disambiguatedName("instance", id, instanceIds, scope)}
              onChange={(v) => patchUrl({ issueInstance: v })}
            />
            <DiagnosticFilterSelect
              label="GPU"
              value={url.issueGpu}
              options={gpuIds}
              resolveLabel={(id) => displayNameFor("gpu", id, scope)}
              onChange={(v) => patchUrl({ issueGpu: v })}
            />
            <DiagnosticFilterSelect
              label="code"
              value={url.issueCode}
              options={codes}
              resolveLabel={(code) => code}
              onChange={(v) => patchUrl({ issueCode: v })}
            />
          </div>
        </FilterPanel>
        {(activeFilterCount > 0 || url.issueSeverity !== "all") && (
          <button
            type="button"
            onClick={clearConditions}
            className="text-2xs text-brand no-underline hover:underline"
          >
            清除条件（保留搜索）
          </button>
        )}
      </div>

      {activeFilterCount > 0 && (
        <div className="flex flex-wrap gap-1.5" role="group" aria-label="已应用的诊断筛选">
          {(
            [
              ["issuePool", "服务池", "pool"],
              ["issueInstance", "实例", "instance"],
              ["issueGpu", "GPU", "gpu"],
              ["issueCode", "code", null],
            ] as const
          ).map(([key, label, kind]) => {
            const value = url[key];
            if (!value) return null;
            return (
              <ActiveFilterChip
                key={key}
                label={`${label}：${displayNameForKind(kind, value, scope)}`}
                onRemove={() => patchUrl({ [key]: "" })}
              />
            );
          })}
        </div>
      )}
      <UrlIssueChips
        issues={urlIssuesForView(urlIssues, "issues")}
        onDismiss={(key) => {
          if (key === "issue_severity") patchUrl({ issueSeverity: "all" });
        }}
      />

      {/* Diagnostic list */}
      {filtered.length === 0 ? (
        <EmptyState
          icon="checkCircle"
          message={sorted.length === 0 ? "当前没有诊断告警" : "没有匹配的诊断"}
          hint={
            sorted.length === 0
              ? "服务池、实例、GPU 资源的状态均无 critical / blocker 级问题。"
              : undefined
          }
        />
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {filtered.map((d) => (
            <DiagnosticRow key={d.id} diagnostic={d} scope={scope} patchUrl={patchUrl} />
          ))}
        </ul>
      )}
    </div>
  );
}

// ── 名称解析（plan §4.3：可读名称优先，短 ID 辅助） ─────────────────────────

type FocusKind = "pool" | "instance" | "gpu";

function objectExists(kind: FocusKind, id: string, scope: RegistrySectionProps["scope"]): boolean {
  switch (kind) {
    case "pool":
      return scope.vm.pools.some((p) => p.id === id);
    case "instance":
      return scope.vm.pools.some((p) => p.members.some((m) => m.registry_id === id));
    case "gpu":
      return (scope.gpuResources ?? []).some((r) => r.gpu_resource_id === id);
  }
}

function displayNameForKind(
  kind: FocusKind | null,
  id: string,
  scope: RegistrySectionProps["scope"],
): string {
  if (kind === null) return id;
  return displayNameFor(kind, id, scope);
}

/** Readable label for a stable id: 名称优先，重名/无名时附短 ID。 */
function displayNameFor(kind: FocusKind, id: string, scope: RegistrySectionProps["scope"]): string {
  if (kind === "pool") {
    const pool = scope.vm.pools.find((p) => p.id === id);
    return pool ? pool.name : `服务池 ${formatShortId(id)}`;
  }
  if (kind === "instance") {
    for (const pool of scope.vm.pools) {
      const m = pool.members.find((x) => x.registry_id === id);
      if (m) return m.name;
    }
    return `实例 ${formatShortId(id)}`;
  }
  const gpu = (scope.gpuResources ?? []).find((r) => r.gpu_resource_id === id);
  if (gpu?.physical_device_token) return gpu.physical_device_token;
  return formatShortId(id);
}

/** 重名时给选项补短 ID，实际筛选值仍是稳定 ID（plan §4.3）。 */
function disambiguatedName(
  kind: FocusKind,
  id: string,
  allIds: string[],
  scope: RegistrySectionProps["scope"],
): string {
  const base = displayNameFor(kind, id, scope);
  if (kind === "gpu") return base;
  const sameNamed = allIds.filter((other) => displayNameFor(kind, other, scope) === base);
  return sameNamed.length > 1 ? `${base}（${formatShortId(id)}）` : base;
}

// ── 行渲染 ───────────────────────────────────────────────────────────────────

function DiagnosticRow({
  diagnostic,
  scope,
  patchUrl,
}: {
  diagnostic: Diagnostic;
  scope: RegistrySectionProps["scope"];
  patchUrl: RegistrySectionProps["patchUrl"];
}): ReactNode {
  const subjectKind: FocusKind | null =
    diagnostic.subject_type === "service_pool"
      ? "pool"
      : diagnostic.subject_type === "instance"
        ? "instance"
        : diagnostic.subject_type === "gpu_resource"
          ? "gpu"
          : null;
  const subjectLabel =
    diagnostic.subject_type === "service_pool"
      ? `服务池「${displayNameFor("pool", diagnostic.subject_id, scope)}」`
      : diagnostic.subject_type === "instance"
        ? `实例「${displayNameFor("instance", diagnostic.subject_id, scope)}」`
        : diagnostic.subject_type === "gpu_resource"
          ? `GPU ${displayNameFor("gpu", diagnostic.subject_id, scope)}`
          : `模型池 ${diagnostic.subject_id}`;
  const subjectResolvable =
    subjectKind !== null && objectExists(subjectKind, diagnostic.subject_id, scope);
  const sevToken = SEVERITY_TOKENS[diagnostic.severity];

  const navigateTo = (kind: FocusKind, id: string) =>
    patchUrl(focusPatchFor(kind, id), { replace: false });

  return (
    <li className="flex flex-col gap-1.5 rounded-md border border-border bg-card px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-2">
        {/* Severity badge: icon + label only. The full message is rendered
            in the body below — we must NOT duplicate it here (plan §7.1:
            one primary record per code+subject). */}
        <Badge variant={sevToken.variant}>
          <Icon name={sevToken.icon} size={11} />
          <span>{sevToken.label}</span>
        </Badge>
        <Badge variant="outline" className="text-2xs">
          {diagnostic.code}
        </Badge>
        <span className="text-2xs text-muted-foreground">{diagnostic.source}</span>
      </div>
      <div className="text-sm">{diagnostic.message}</div>
      {diagnostic.remediation && (
        <div className="flex items-start gap-1.5 text-2xs text-muted-foreground">
          <Icon name="info" size={11} className="mt-0.5 flex-shrink-0" />
          <span>建议 · {diagnostic.remediation}</span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-muted-foreground">
        <span className="inline-flex items-center gap-1.5">
          对象 · {subjectLabel}
          {subjectResolvable && subjectKind && (
            <button
              type="button"
              onClick={() => navigateTo(subjectKind, diagnostic.subject_id)}
              className="cursor-pointer text-brand no-underline hover:underline"
            >
              定位
            </button>
          )}
        </span>
        {diagnostic.affected_service_pool_ids.length > 0 && (
          <AffectedGroup
            kind="pool"
            label="服务池"
            ids={diagnostic.affected_service_pool_ids}
            scope={scope}
            onNavigate={navigateTo}
          />
        )}
        {diagnostic.affected_instance_ids.length > 0 && (
          <AffectedGroup
            kind="instance"
            label="实例"
            ids={diagnostic.affected_instance_ids}
            scope={scope}
            onNavigate={navigateTo}
          />
        )}
        {diagnostic.affected_gpu_resource_ids.length > 0 && (
          <AffectedGroup
            kind="gpu"
            label="GPU"
            ids={diagnostic.affected_gpu_resource_ids}
            scope={scope}
            onNavigate={navigateTo}
          />
        )}
      </div>
    </li>
  );
}

/**
 * 影响对象：显示可读名称并可跳转到对应子页定位；源数据没有可匹配记录时只给
 * 非交互摘要，不制造禁用的伪跳转（plan §4.3）。
 */
function AffectedGroup({
  kind,
  label,
  ids,
  scope,
  onNavigate,
}: {
  kind: FocusKind;
  label: string;
  ids: string[];
  scope: RegistrySectionProps["scope"];
  onNavigate: (kind: FocusKind, id: string) => void;
}): ReactNode {
  const resolvable = ids.filter((id) => objectExists(kind, id, scope));
  const unresolvable = ids.length - resolvable.length;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <span>
        {label} {ids.length}
      </span>
      {resolvable.slice(0, 4).map((id) => (
        <button
          key={id}
          type="button"
          title={`定位到 ${label} ${id}`}
          onClick={() => onNavigate(kind, id)}
          className="cursor-pointer rounded border border-border bg-background px-1.5 py-0.5 text-2xs text-foreground no-underline hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {displayNameFor(kind, id, scope)}
        </button>
      ))}
      {resolvable.length > 4 && (
        <AffectedGroupOverflow
          kind={kind}
          ids={resolvable.slice(4)}
          scope={scope}
          onNavigate={onNavigate}
        />
      )}
      {unresolvable > 0 && <span>另 {unresolvable} 个对象不可达</span>}
    </span>
  );
}

function AffectedGroupOverflow({
  kind,
  ids,
  scope,
  onNavigate,
}: {
  kind: FocusKind;
  ids: string[];
  scope: RegistrySectionProps["scope"];
  onNavigate: (kind: FocusKind, id: string) => void;
}): ReactNode {
  const [open, setOpen] = useState(false);
  // 溢出对象默认折叠；展开即平铺全部名称按钮，避免首屏被长列表撑开。
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="cursor-pointer text-brand no-underline hover:underline"
      >
        展开 {ids.length} 个
      </button>
    );
  }
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      {ids.map((id) => (
        <button
          key={id}
          type="button"
          title={`定位到 ${id}`}
          onClick={() => onNavigate(kind, id)}
          className="cursor-pointer rounded border border-border bg-background px-1.5 py-0.5 text-2xs text-foreground no-underline hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          {displayNameFor(kind, id, scope)}
        </button>
      ))}
      <button
        type="button"
        onClick={() => setOpen(false)}
        className="cursor-pointer text-muted-foreground no-underline hover:text-foreground"
      >
        收起
      </button>
    </span>
  );
}

function SummaryBadge({
  label,
  count,
  variant,
}: {
  label: string;
  count: number;
  variant: "danger" | "warning" | "accent" | "outline";
}): ReactNode {
  return (
    <Badge variant={variant}>
      <span>{label}</span>
      <span className="font-semibold">{count}</span>
    </Badge>
  );
}

function DiagnosticFilterSelect({
  label,
  value,
  options,
  resolveLabel,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  resolveLabel: (id: string) => string;
  onChange: (v: string) => void;
}): ReactNode {
  return (
    <label className="flex flex-col gap-1.5 text-xs text-muted-foreground">
      {label}
      <FilterSelect
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full"
        aria-label={`按 ${label} 筛选`}
      >
        <option value="">全部</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {resolveLabel(o)}
          </option>
        ))}
      </FilterSelect>
    </label>
  );
}
