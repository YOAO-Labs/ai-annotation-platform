import { CopyableError } from "@/components/ui/CopyableError";

import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery, useQueries, useQueryClient } from "@tanstack/react-query";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { adminMlIntegrationsApi, type MLBackendItem } from "@/api/adminMlIntegrations";
import { mlBackendsApi, type MLBackendCapability, type MLModelCapability } from "@/api/ml-backends";
import {
  useProtocolCapabilities,
  useCapabilityInstances,
  type CapabilityInstance,
  type CapabilityInstanceModel,
} from "@/api/mlCapabilities";
import { usePermissions } from "@/hooks/usePermissions";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { ProtocolCapabilityCard } from "./ProtocolCapabilityCard";
import { EmptyCatalogBanner } from "./EmptyCatalogBanner";
import { taskLabel, infraLabel, modalityLabel } from "./capability/labels";
import type { FlatModel, CatalogViewMode, CatalogGroupBy } from "./capability/types";
import {
  effectiveInfra,
  effectiveModalities,
  groupModels,
  toggle,
} from "./capability/catalogModel";
import { CATALOG_URL_DEFAULTS, MARKET_URL_KEYS, catalogUrlCodec } from "./marketUrlState";
import { FilterToolbar } from "./capability/FilterToolbar";
import { ModelListTable } from "./capability/ModelListTable";
import { ModelCard } from "./capability/ModelCard";
import { ProtocolCapabilityListTable } from "./ProtocolCapabilityListTable";

const NOTE_CLASS = "p-4 text-xs text-muted-foreground";
const EMPTY_STATE_CLASS =
  "flex flex-col items-center gap-1.5 p-8 text-center text-sm text-muted-foreground";
const RETRY_BTN_CLASS =
  "ml-1 cursor-pointer appearance-none rounded-md border border-border bg-card px-3 py-1 text-xs text-foreground";
const SELECT_CLASS =
  "appearance-none rounded-md border border-border bg-muted px-2 py-1 text-xs text-foreground";
const VIEW_BTN_CLASS =
  "inline-flex h-[26px] w-7 cursor-pointer appearance-none items-center justify-center rounded-sm border-0 bg-transparent text-muted-foreground hover:bg-muted hover:text-foreground";
const VIEW_BTN_ON_CLASS =
  "inline-flex h-[26px] w-7 cursor-pointer appearance-none items-center justify-center rounded-sm border-0 bg-card text-foreground ring-1 ring-border";

interface BackendResult {
  backend: MLBackendItem;
  projectName: string;
  data?: MLBackendCapability;
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
}

function instanceModelToFlat(
  inst: CapabilityInstance,
  m: CapabilityInstanceModel,
  source: FlatModel["source"],
): FlatModel {
  const infraFallback = inst.infra && inst.infra !== "unknown" ? inst.infra : undefined;
  return {
    model: {
      id: m.id,
      display_name: m.display_name,
      task: m.task,
      model_family: m.model_family ?? undefined,
      composition: m.composition,
      infra: m.infra ?? infraFallback,
      is_interactive: m.is_interactive,
      supported_prompts: m.supported_prompts,
      supported_inputs: m.supported_inputs,
      resource_profile: m.resource_profile,
      supported_geometric_outputs: m.supported_geometric_outputs,
      output_attribute_types: m.output_attribute_types,
      output_attribute_schema: m.output_attribute_schema,
      supported_trackers: m.supported_trackers,
      supported_variants: m.supported_variants,
      variant_combinations: m.variant_combinations,
      variants_shared_across_tasks: m.variants_shared_across_tasks,
      default_variants: m.default_variants,
      modality: m.modality ?? undefined,
    },
    backendId: source === "env_only" ? `env-only:${inst.name}` : `instance:${inst.name}`,
    backendName: inst.name,
    projectId: "",
    projectName: source === "env_only" ? "平台内置" : "已注册",
    source,
    registeredProjects: [],
    backendInfra: infraFallback,
    backendModalities: m.modality ? [m.modality] : undefined,
    healthMeta: undefined,
    warmupEndpoint: inst.warmup_endpoint ?? false,
    stale: false,
  };
}

export function CapabilityCatalogPanel() {
  const qc = useQueryClient();
  const pushToast = useToastStore((s) => s.push);
  const [searchParams, setSearchParams] = useSearchParams();
  const [refreshing, setRefreshing] = useState(false);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set());

  const {
    state: catalog,
    patch: patchCatalog,
    issues: catalogIssues,
  } = useUrlFilterState({ codec: catalogUrlCodec, defaults: CATALOG_URL_DEFAULTS });
  const search = catalog.catalogQ;
  const viewMode = catalog.catalogView as CatalogViewMode;
  const groupBy = catalog.catalogGroup as CatalogGroupBy;
  const taskFilter = useMemo(() => new Set(catalog.catalogTask), [catalog.catalogTask]);
  const familyFilter = useMemo(() => new Set(catalog.catalogFamily), [catalog.catalogFamily]);
  const infraFilter = useMemo(() => new Set(catalog.catalogInfra), [catalog.catalogInfra]);
  const modalityFilter = useMemo(() => new Set(catalog.catalogModality), [catalog.catalogModality]);

  const { data: protocol } = useProtocolCapabilities();
  const { data: instancesData } = useCapabilityInstances();

  const goToRegistry = () => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "registry");
    setSearchParams(next, { replace: false });
  };

  const { role } = usePermissions();
  const isSuperAdmin = role === "super_admin";

  const {
    data: overview,
    isLoading: overviewLoading,
    isError: overviewError,
    error: overviewErr,
    refetch: refetchOverview,
  } = useQuery({
    queryKey: ["admin", "ml-integrations", "overview"],
    queryFn: () => adminMlIntegrationsApi.overview(),
    refetchInterval: 60_000,
    enabled: isSuperAdmin,
  });

  const backendRefs = useMemo(() => {
    const byUrl = new Map<
      string,
      { backend: MLBackendItem; projectName: string; registeredProjects: string[] }
    >();
    for (const p of overview?.projects ?? []) {
      for (const b of p.backends) {
        const key = b.url.replace(/\/+$/, "");
        const existing = byUrl.get(key);
        if (existing) {
          if (!existing.registeredProjects.includes(p.project_name)) {
            existing.registeredProjects.push(p.project_name);
          }
        } else {
          byUrl.set(key, {
            backend: b,
            projectName: p.project_name,
            registeredProjects: [p.project_name],
          });
        }
      }
    }
    return [...byUrl.values()];
  }, [overview]);

  const registeredBackendIds = useMemo(
    () => new Set(backendRefs.map((r) => r.backend.id)),
    [backendRefs],
  );

  const capabilityQueries = useQueries({
    queries: backendRefs.map((ref) => ({
      queryKey: ["ml-backend-capabilities", ref.backend.project_id, ref.backend.id],
      queryFn: () => mlBackendsApi.capabilities(ref.backend.project_id, ref.backend.id),
      staleTime: 60_000,
    })),
  });

  const results: (BackendResult & { registeredProjects: string[] })[] = backendRefs.map(
    (ref, i) => {
      const q = capabilityQueries[i];
      return {
        backend: ref.backend,
        projectName: ref.projectName,
        registeredProjects: ref.registeredProjects,
        data: q?.data,
        isLoading: q?.isLoading ?? false,
        isError: q?.isError ?? false,
        error: q?.error,
      };
    },
  );

  const flatModels: FlatModel[] = useMemo(() => {
    const out: FlatModel[] = [];
    const seenBackendIds = new Set<string>();
    for (const r of results) {
      if (!r.data) continue;
      seenBackendIds.add(r.backend.id);
      const cap = r.data;
      const models: MLModelCapability[] =
        cap.models && cap.models.length > 0
          ? cap.models
          : [
              {
                id: r.backend.id,
                display_name: cap.name ?? r.backend.name,
                is_interactive: cap.is_interactive,
                supported_prompts: cap.supported_prompts,
                supported_geometric_outputs: cap.supported_geometric_outputs,
                supported_text_outputs: cap.supported_text_outputs,
                supported_trackers: cap.supported_trackers,
                supported_variants: cap.supported_variants,
                infra: cap.infra,
              },
            ];
      const stale = r.backend.state !== "connected";
      const originalBackendName = cap.name || r.backend.name;
      for (const m of models) {
        out.push({
          model: m,
          backendId: r.backend.id,
          backendName: originalBackendName,
          projectId: r.backend.project_id,
          projectName: r.projectName,
          source: "registered",
          registeredProjects: r.registeredProjects,
          backendInfra: cap.infra,
          backendModalities: cap.modalities,
          healthMeta: r.backend.health_meta,
          warmupEndpoint:
            cap.warmup_endpoint ?? r.backend.health_meta?.capabilities?.warmup_endpoint,
          stale,
          warnings: cap.warnings?.filter((w) => w.model_id === m.id),
        });
      }
    }
    for (const inst of instancesData?.instances ?? []) {
      if (seenBackendIds.has(inst.backend_id)) continue;
      seenBackendIds.add(inst.backend_id);
      for (const m of inst.models) {
        out.push(instanceModelToFlat(inst, m, "env_only"));
      }
    }
    return out;
  }, [results, instancesData]);

  const facets = useMemo(() => {
    const tasks = new Set<string>();
    const families = new Set<string>();
    const infras = new Set<string>();
    const modalities = new Set<string>();
    for (const f of flatModels) {
      if (f.model.task) tasks.add(f.model.task);
      if (f.model.model_family) families.add(f.model.model_family);
      const inf = effectiveInfra(f.model, f.backendInfra);
      if (inf) infras.add(inf);
      for (const mod of effectiveModalities(f.model, f.backendModalities)) modalities.add(mod);
    }
    return {
      tasks: [...tasks].sort(),
      families: [...families].sort(),
      infras: [...infras].sort(),
      modalities: [...modalities].sort(),
    };
  }, [flatModels]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLocaleLowerCase();
    return flatModels.filter((f) => {
      if (taskFilter.size > 0 && !(f.model.task && taskFilter.has(f.model.task))) return false;
      if (
        familyFilter.size > 0 &&
        !(f.model.model_family && familyFilter.has(f.model.model_family))
      )
        return false;
      if (infraFilter.size > 0) {
        const inf = effectiveInfra(f.model, f.backendInfra);
        if (!inf || !infraFilter.has(inf)) return false;
      }
      if (modalityFilter.size > 0) {
        const mods = effectiveModalities(f.model, f.backendModalities);
        if (!mods.some((m) => modalityFilter.has(m))) return false;
      }
      if (needle) {
        const haystack = [
          f.model.display_name,
          f.model.id,
          f.model.model_family,
          f.model.task ? taskLabel(f.model.task) : "",
          f.backendName,
          f.projectName,
        ]
          .filter(Boolean)
          .join(" ")
          .toLocaleLowerCase();
        if (!haystack.includes(needle)) return false;
      }
      return true;
    });
  }, [flatModels, taskFilter, familyFilter, infraFilter, modalityFilter, search]);

  const grouped = useMemo(() => groupModels(filtered, groupBy), [filtered, groupBy]);

  const flatByKey = useMemo(() => {
    const map = new Map<string, FlatModel>();
    for (const f of flatModels) map.set(`${f.backendName}::${f.model.id}`, f);
    return map;
  }, [flatModels]);

  const protocolView = useMemo(() => {
    if (!protocol) return null;
    const needle = search.trim().toLocaleLowerCase();
    const byTask = new Map<string, FlatModel[]>();
    for (const inst of instancesData?.instances ?? []) {
      for (const m of inst.models) {
        const taskId = m.task ?? "unknown";
        const source: FlatModel["source"] = registeredBackendIds.has(inst.backend_id)
          ? "registered"
          : "env_only";
        const enriched =
          flatByKey.get(`${inst.name}::${m.id}`) ?? instanceModelToFlat(inst, m, source);
        if (
          familyFilter.size > 0 &&
          !(enriched.model.model_family && familyFilter.has(enriched.model.model_family))
        ) {
          continue;
        }
        if (infraFilter.size > 0) {
          const eff = effectiveInfra(enriched.model, enriched.backendInfra);
          if (!eff || !infraFilter.has(eff)) continue;
        }
        if (modalityFilter.size > 0) {
          const mods = effectiveModalities(enriched.model, enriched.backendModalities);
          if (!mods.some((modality) => modalityFilter.has(modality))) continue;
        }
        if (!byTask.has(taskId)) byTask.set(taskId, []);
        byTask.get(taskId)!.push(enriched);
      }
    }
    const hasModelFilter = familyFilter.size > 0 || infraFilter.size > 0 || modalityFilter.size > 0;
    return protocol.tasks
      .filter((task) => {
        if (taskFilter.size > 0 && !taskFilter.has(task.id)) return false;
        if (hasModelFilter && (byTask.get(task.id)?.length ?? 0) === 0) return false;
        if (needle) {
          const meta = [task.label, task.id, task.summary, ...task.typical_models]
            .join(" ")
            .toLocaleLowerCase();
          if (!meta.includes(needle) && (byTask.get(task.id)?.length ?? 0) === 0) {
            return false;
          }
        }
        return true;
      })
      .map((task) => ({ task, mounted: byTask.get(task.id) ?? [] }));
  }, [
    protocol,
    instancesData,
    flatByKey,
    registeredBackendIds,
    taskFilter,
    familyFilter,
    infraFilter,
    modalityFilter,
    search,
  ]);

  const renderedModelCount =
    groupBy === "task" && protocolView
      ? protocolView.reduce((sum, v) => sum + v.mounted.length, 0)
      : filtered.length;
  const renderedCategoryCount =
    groupBy === "task" && protocolView ? protocolView.length : grouped.length;

  const hasActiveFilter =
    taskFilter.size > 0 || familyFilter.size > 0 || infraFilter.size > 0 || modalityFilter.size > 0;
  const clearFilters = () => {
    patchCatalog({ catalogTask: [], catalogFamily: [], catalogInfra: [], catalogModality: [] });
  };
  const toggleCatalogFacet = (
    key: "catalogTask" | "catalogFamily" | "catalogInfra" | "catalogModality",
    value: string,
  ) => {
    patchCatalog((prev) => {
      const next = new Set(prev[key]);
      if (next.has(value)) next.delete(value);
      else next.add(value);
      return { ...prev, [key]: [...next] };
    });
  };
  const toggleGroup = (key: string) => {
    if (flatModels.length > 30) {
      setExpandedGroups((groups) => toggle(groups, key));
    } else {
      setCollapsedGroups((groups) => toggle(groups, key));
    }
  };

  const onRefresh = async () => {
    if (refreshing || backendRefs.length === 0) return;
    setRefreshing(true);
    const settled = await Promise.allSettled(
      backendRefs.map((ref) =>
        mlBackendsApi.refreshCapabilities(ref.backend.project_id, ref.backend.id),
      ),
    );
    qc.invalidateQueries({ queryKey: ["ml-backend-capabilities"] });
    qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "overview"] });
    setRefreshing(false);
    const failed = settled.filter((s) => s.status === "rejected").length;
    pushToast(
      failed === 0
        ? { msg: `已刷新 ${settled.length} 个模型服务的能力目录`, kind: "success" }
        : { msg: `能力刷新完成，${failed}/${settled.length} 个模型服务探测失败`, kind: "warning" },
    );
  };

  const anyCapLoading = results.some((r) => r.isLoading);
  const envOnlyCount = (instancesData?.instances ?? []).filter(
    (i) => !registeredBackendIds.has(i.backend_id),
  ).length;
  const distinctBackendCount = backendRefs.length + envOnlyCount;

  const primaryToolbar = (
    <div className="flex flex-wrap items-center gap-2.5 border-b border-border px-4 py-3">
      <label className="inline-flex min-w-[220px] flex-[1_1_280px] items-center gap-2 rounded-md border border-border bg-muted px-2.5 py-1.5 text-muted-foreground">
        <Icon name="search" size={13} />
        <input
          value={search}
          onChange={(e) => patchCatalog({ catalogQ: e.target.value })}
          placeholder="搜索模型、ID、模型族、任务或来源"
          className="w-full min-w-0 appearance-none border-0 bg-transparent text-xs text-foreground outline-none"
        />
      </label>
      <div className="inline-flex gap-1 rounded-md border border-border bg-muted p-1">
        <button
          type="button"
          className={viewMode === "cards" ? VIEW_BTN_ON_CLASS : VIEW_BTN_CLASS}
          onClick={() => patchCatalog({ catalogView: "cards" })}
          aria-pressed={viewMode === "cards"}
          title="卡片视图"
        >
          <Icon name="grid" size={13} />
        </button>
        <button
          type="button"
          className={viewMode === "list" ? VIEW_BTN_ON_CLASS : VIEW_BTN_CLASS}
          onClick={() => patchCatalog({ catalogView: "list" })}
          aria-pressed={viewMode === "list"}
          title="列表视图"
        >
          <Icon name="list" size={13} />
        </button>
      </div>
      <label className="inline-flex items-center gap-1.5 text-xs font-semibold text-muted-foreground">
        分组
        <select
          value={groupBy}
          onChange={(e) => patchCatalog({ catalogGroup: e.target.value as CatalogGroupBy })}
          className={SELECT_CLASS}
        >
          <option value="task">协议能力</option>
          <option value="backend">模型服务</option>
          <option value="infra">推理框架</option>
          <option value="none">不分组</option>
        </select>
      </label>
      <Button
        size="sm"
        className="ml-auto"
        onClick={onRefresh}
        disabled={refreshing || backendRefs.length === 0}
        title="重新探测已在项目中启用的模型服务并刷新能力目录"
      >
        <Icon name="refresh" size={11} className={refreshing ? "spin" : undefined} />
        刷新
      </Button>
    </div>
  );

  return (
    <div className="mb-4">
      <Card>
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <div className="flex items-center gap-2">
            <Icon name="layers" size={14} className="text-muted-foreground" />
            <h3 className="m-0 text-sm font-semibold">能力目录</h3>
            <span className="text-xs text-muted-foreground">
              {groupBy === "task" && protocolView
                ? `${renderedCategoryCount} 类协议能力 · 匹配 ${renderedModelCount} / 总计 ${flatModels.length} 个模型条目 · ${distinctBackendCount} 个模型服务`
                : `匹配 ${renderedModelCount} / 总计 ${flatModels.length} 个模型条目 · ${distinctBackendCount} 个模型服务`}
            </span>
          </div>
        </div>

        {overviewLoading ? (
          <div className={NOTE_CLASS}>正在读取模型服务列表…</div>
        ) : overviewError ? (
          <div className="flex items-center gap-2 px-4 py-3 text-xs text-status-danger">
            加载失败：
            <CopyableError message={(overviewErr as Error)?.message ?? "未知错误"} />
            <button className={RETRY_BTN_CLASS} onClick={() => refetchOverview()}>
              重试
            </button>
          </div>
        ) : backendRefs.length === 0 &&
          (instancesData?.instances ?? []).length === 0 &&
          groupBy !== "task" ? (
          <>
            {primaryToolbar}
            <div className={EMPTY_STATE_CLASS}>
              <Icon name="layers" size={28} className="opacity-30" />
              <div>尚无项目注册模型服务</div>
              <div className="text-xs">
                切到「分组: task」可查看平台协议层支持的全部能力； 或在项目设置注册 backend 后,
                其能力目录会出现在这里。
              </div>
            </div>
          </>
        ) : (
          <>
            {primaryToolbar}

            <FilterToolbar
              facets={facets}
              taskFilter={taskFilter}
              familyFilter={familyFilter}
              infraFilter={infraFilter}
              modalityFilter={modalityFilter}
              onToggleTask={(v) => toggleCatalogFacet("catalogTask", v)}
              onToggleFamily={(v) => toggleCatalogFacet("catalogFamily", v)}
              onToggleInfra={(v) => toggleCatalogFacet("catalogInfra", v)}
              onToggleModality={(v) => toggleCatalogFacet("catalogModality", v)}
              hasActiveFilter={hasActiveFilter}
              onClear={clearFilters}
              issues={catalogIssues}
              onDismissIssue={(key) => {
                if (key === MARKET_URL_KEYS.catalogGroup) {
                  patchCatalog({ catalogGroup: CATALOG_URL_DEFAULTS.catalogGroup });
                } else if (key === MARKET_URL_KEYS.catalogView) {
                  patchCatalog({ catalogView: CATALOG_URL_DEFAULTS.catalogView });
                }
              }}
              matchedCount={renderedModelCount}
              totalCount={flatModels.length}
              categoryCount={groupBy === "task" && protocolView ? renderedCategoryCount : null}
            />

            {results.some((r) => r.isError) && (
              <div className="mx-4 mt-3 flex items-center gap-2 rounded-md border border-amber-500/30 bg-status-caution-soft px-3 py-2 text-xs text-foreground">
                <Icon name="warning" size={13} />
                <span>
                  {results.filter((r) => r.isError).length} 个模型服务的能力读取失败，
                  目录可能不完整；点击「刷新」可重新探测。
                </span>
              </div>
            )}

            {anyCapLoading && flatModels.length === 0 && groupBy !== "task" ? (
              <div className={NOTE_CLASS}>正在读取模型服务的能力…</div>
            ) : groupBy === "task" && protocolView ? (
              <>
                {protocolView.every((v) => v.mounted.length === 0) && (
                  <EmptyCatalogBanner
                    taskCount={protocolView.length}
                    onGoToRegistry={goToRegistry}
                  />
                )}
                {protocolView.length === 0 ? (
                  <div className={EMPTY_STATE_CLASS}>
                    <Icon name="filter" size={24} className="opacity-30" />
                    <div>当前过滤条件无匹配能力</div>
                    {hasActiveFilter && (
                      <button className={RETRY_BTN_CLASS} onClick={clearFilters}>
                        清除过滤
                      </button>
                    )}
                  </div>
                ) : viewMode === "cards" ? (
                  <div className="flex flex-col gap-3 p-4">
                    {protocolView.map(({ task, mounted }) => (
                      <ProtocolCapabilityCard
                        key={task.id}
                        task={task}
                        mounted={mounted}
                        infraLabel={infraLabel}
                        modalityLabel={modalityLabel}
                        onGoToRegistry={goToRegistry}
                      />
                    ))}
                  </div>
                ) : (
                  <div className="p-4">
                    <ProtocolCapabilityListTable
                      rows={protocolView}
                      infraLabel={infraLabel}
                      modalityLabel={modalityLabel}
                      onGoToRegistry={goToRegistry}
                    />
                  </div>
                )}
              </>
            ) : filtered.length === 0 ? (
              <div className={EMPTY_STATE_CLASS}>
                <Icon name="filter" size={24} className="opacity-30" />
                <div>
                  {hasActiveFilter || search ? "当前过滤条件无匹配模型" : "暂无可用模型条目"}
                </div>
                {(hasActiveFilter || search) && (
                  <div className="flex gap-2">
                    {hasActiveFilter && (
                      <button className={RETRY_BTN_CLASS} onClick={clearFilters}>
                        清除过滤
                      </button>
                    )}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-3 p-4">
                {grouped.map((group) => {
                  const defaultCollapsed = flatModels.length > 30 && groupBy !== "none";
                  const collapsed =
                    groupBy !== "none" &&
                    (defaultCollapsed
                      ? !expandedGroups.has(group.key)
                      : collapsedGroups.has(group.key));
                  return (
                    <section key={group.key} className="min-w-0">
                      {groupBy !== "none" && (
                        <button
                          type="button"
                          className="mb-2 flex w-full cursor-pointer appearance-none items-center gap-1.5 rounded-md border border-border bg-muted px-2 py-1.5 text-xs font-semibold text-foreground"
                          onClick={() => toggleGroup(group.key)}
                          aria-expanded={!collapsed}
                        >
                          <Icon name={collapsed ? "chevRight" : "chevDown"} size={13} />
                          <span>{group.label}</span>
                          <span className="ml-auto text-xs text-muted-foreground">
                            {group.items.length}
                          </span>
                        </button>
                      )}
                      {!collapsed &&
                        (viewMode === "cards" ? (
                          <div className="grid grid-cols-[repeat(auto-fill,minmax(300px,1fr))] gap-3">
                            {group.items.map((f) => (
                              <ModelCard
                                key={`${f.backendId}:${f.model.id}`}
                                item={f}
                                showTaskBadge={groupBy !== "task"}
                              />
                            ))}
                          </div>
                        ) : (
                          <ModelListTable items={group.items} />
                        ))}
                    </section>
                  );
                })}
              </div>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
