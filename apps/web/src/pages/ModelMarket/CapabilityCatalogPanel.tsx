// v0.14.9 · 模型市场「能力目录」面板 — 能力声明协议 v2 的消费视图.
// 与「项目级 ML Backend」表格不同, 这里按 *model 条目* 展开:
//   - 枚举所有项目已注册 backend (admin overview), 对每个 backend 拉 /capabilities 拿 models[];
//   - 每个 model 渲染一张卡片 (task/infra/modality badge + 输出几何 + 输出属性 + variants + resource);
//   - 老 backend (协议 v1) 由平台合成单 model, models 长度=1, 正常显示;
//   - 工具栏按 task / model_family / infra / modality 多选 chips 过滤;
//   - 「刷新」对每个 backend 调 refreshCapabilities 重探并刷新缓存.
// 仅消费已落地契约 (api/ml-backends.ts + adminMlIntegrations.ts), 不改 api / types.
//
// 模型市场多 TAB UI 优化 · 阶段二 (plan §4.1 / §5):
//   - 工具栏固定为三行: ①搜索/分组/卡片列表/刷新 ②任务/模态快捷筛选+更多筛选
//     ③结果计数+已应用条件标签; 全部条件写入 URL (catalog_* 键, 多选重复键)。
//   - 计数唯一来源: 「匹配 N / 总计 M」来自当前实际渲染集合, 不在父页面按项目
//     累加第二套模型总数; 协议能力类别数单独标注, 不与模型条目数混用。

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

// 单个 backend 的 capability 拉取结果 (含失败态供降级提示).
interface BackendResult {
  backend: MLBackendItem;
  projectName: string;
  data?: MLBackendCapability;
  isLoading: boolean;
  isError: boolean;
  error?: unknown;
}

// 协议卡视图把 instances 端点的 model 合成 FlatModel, 以便复用 ModelCard 渲染 (与其他
// 分组视图对齐)。instances 端点不带运行时池 (池大小 / 加载态在卡上显示占位); resource_profile
// 自 v0.19.2 WS0 起已透传, 故「可批量 / 设备 / 资源」徽标可直接渲染。当 admin 的 flatModels
// (来自 /capabilities) 有同名条目时, 上层会优先用富数据 (带运行时池)。
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
      // 属性两字段都原样透传 (不在此投影): ModelCard「输出属性」行统一判定
      // ——schema 非空取其 label/key, 否则回落扁平 output_attribute_types。此前这里只从 schema
      // 投影, 丢掉了 gsam2 / sam3 / yolo 只报的 output_attribute_types (它们 schema 为空),
      // 令未接入项目的 backend (instances 路径) 属性行空成「—」。
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

  // 目录条件全部写入 URL（plan §5 catalog_* 键表）：文本编辑 replace；分组/视图
  // 切换是显式显示状态，仍 replace（主 TAB 语义才 push）。
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

  // v0.14.11 · 协议级能力目录 (与 backend 注册解耦); 用作 groupBy=task 时的协议卡数据源。
  const { data: protocol } = useProtocolCapabilities();
  // v0.14.11 · 平台已知 backend 实例 (env-only + 项目级注册合并, 登录用户可访问);
  // 协议卡视图直接消费这个端点, 不再依赖 admin overview, 让普通用户也能看到 model 清单。
  const { data: instancesData } = useCapabilityInstances();

  const goToRegistry = () => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", "registry");
    // 显式 TAB 跳转新增浏览器历史（plan §5）。
    setSearchParams(next, { replace: false });
  };

  // overview (admin) 只有 super_admin 能读; project_admin 也能进本页, 但对其而言
  // overview 是「可选的运行时富化」——拿不到就退到 /instances 单端点视图 (协议卡 + model 卡),
  // 而不是整块报错。故按角色 enabled, 非超管不发请求、overviewError 不触发。
  const { role } = usePermissions();
  const isSuperAdmin = role === "super_admin";

  // 1) 枚举所有项目 + 已注册 backend.
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

  // v0.14.12 · 按 URL 合并跨项目注册. 同一 ML backend (相同 URL) 在 N 个项目下都
  // 注册过时, 这里只保留首条用于 capabilities 探测, 其余的项目名收入 registeredProjects。
  // 避免: ① 重复显示同名 group; ② 重复打 /capabilities (浪费请求 + 后端 rate limit)。
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

  // overview 只含「项目已启用」的关联; 这里收敛出已启用 backend 的 registry id 集合,
  // 供 ① flatModels env 兜底按 registry id 去重; ② 协议卡回落判定 source (registered / 平台内置)。
  const registeredBackendIds = useMemo(
    () => new Set(backendRefs.map((r) => r.backend.id)),
    [backendRefs],
  );

  // 2) 对每个 backend 拉 /capabilities (独立 query, 互不阻塞).
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

  // 3) 展开为 FlatModel 列表 (合成单 model 的老 backend 也走同一路径).
  //    v0.14.12 · 同时合并 instancesData 中的 env-only 实例 (如 yolo-backend),
  //    让 groupBy=backend / infra / none 视图也能看到 docker-compose 自带 backend,
  //    而不仅在协议卡视图 (groupBy=task) 出现。
  const flatModels: FlatModel[] = useMemo(() => {
    const out: FlatModel[] = [];
    const seenBackendIds = new Set<string>();
    for (const r of results) {
      if (!r.data) continue;
      seenBackendIds.add(r.backend.id);
      const cap = r.data;
      // capabilities 端点对老 backend 会合成 models[]; 若仍为空, 兜底从顶层字段合成一个.
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
      // stale: backend 离线 / 上次探测失败时, 目录可能是缓存旧值.
      const stale = r.backend.state !== "connected";
      // v0.14.12 · backendName 用 cap.name (源 backend 自报名, 如 "grounded-sam2-backend")
      // 而非 r.backend.name (用户取的项目别名, 如 "gsam2.1"). 能力目录是对协议层 backend
      // 的展示, 不依附用户的项目命名. 项目别名信息已经在「注册状态」列体现。
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
          // v0.18.29 · 该 model 命中的受控词表越界诊断 (按 model_id 关联)。
          warnings: cap.warnings?.filter((w) => w.model_id === m.id),
        });
      }
    }
    // 平台已知但未接入任何「已启用」项目的 backend (来自 /ml-capabilities/instances)。
    // overview 只含项目已启用的关联, 故项目未启用 backend 时这条兜底让 groupBy=backend /
    // infra / none 视图仍能看到平台内置 backend, 不再空白。按 registry backend_id 去重,
    // 避免与 overview 分支重复 (overview 分支已把注册 backend 的 registry id 记入 seenBackendIds)。
    for (const inst of instancesData?.instances ?? []) {
      if (seenBackendIds.has(inst.backend_id)) continue;
      seenBackendIds.add(inst.backend_id);
      for (const m of inst.models) {
        out.push(instanceModelToFlat(inst, m, "env_only"));
      }
    }
    return out;
  }, [results, instancesData]);

  // 4) 从已加载 model 派生过滤选项.
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

  // 协议卡复用 ModelCard 渲染已接入模型, 故按 (backendName, model.id) 建索引: 当 admin
  // 的 flatModels (来自 /capabilities, 带运行时池 / resource_profile) 有同名条目时优先用富数据,
  // 非 admin 回落到 instances 合成的结构化字段。
  const flatByKey = useMemo(() => {
    const map = new Map<string, FlatModel>();
    for (const f of flatModels) map.set(`${f.backendName}::${f.model.id}`, f);
    return map;
  }, [flatModels]);

  // v0.14.11 · 协议卡视图: 遍历 protocol.tasks 渲染 9 张卡 (零接入也显示);
  // 数据源是 instances 端点 (env-only + 注册合并, 与 admin overview 完全解耦),
  // 按 model.task 挂到协议卡。search / taskFilter 同时作用于 task 卡过滤。
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

  // 当前实际渲染集合的模型条目数（plan §4.1：计数唯一来源 = 渲染集合）。
  const renderedModelCount =
    groupBy === "task" && protocolView
      ? protocolView.reduce((sum, v) => sum + v.mounted.length, 0)
      : filtered.length;
  const renderedCategoryCount =
    groupBy === "task" && protocolView ? protocolView.length : grouped.length;

  const hasActiveFilter =
    taskFilter.size > 0 || familyFilter.size > 0 || infraFilter.size > 0 || modalityFilter.size > 0;
  // 清除条件保留搜索 / 分组 / 显示方式（plan §5 键表）。
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

  // 5) 刷新: 对每个 backend 调 refreshCapabilities, 再失效对应缓存.
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
        ? { msg: `已重探 ${settled.length} 个 backend 能力目录`, kind: "success" }
        : { msg: `重探完成，${failed}/${settled.length} 个 backend 探测失败`, kind: "warning" },
    );
  };

  const anyCapLoading = results.some((r) => r.isLoading);
  // v0.14.12 · 合并 env 兜底后, 统计与「项目级 backend 计数」分开:
  // 未接入任何已启用项目的平台内置 backend 数 (与 backendRefs 互斥, 不重复计)。
  const envOnlyCount = (instancesData?.instances ?? []).filter(
    (i) => !registeredBackendIds.has(i.backend_id),
  ).length;
  const distinctBackendCount = backendRefs.length + envOnlyCount;

  // ── 工具栏第 1 行：搜索 / 分组 / 卡片列表 / 刷新（plan §4.1 顺序）────────
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
          <option value="task">协议能力 (默认)</option>
          <option value="backend">backend</option>
          <option value="infra">infra</option>
          <option value="none">不分组</option>
        </select>
      </label>
      <Button
        size="sm"
        className="ml-auto"
        onClick={onRefresh}
        disabled={refreshing || backendRefs.length === 0}
        title="对所有 backend 重探 /setup 并刷新能力目录"
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
            {/* 计数唯一来源（plan §4.1）：匹配 / 总计 来自当前渲染集合；
                能力类别数与模型条目数分开表述，不混用。 */}
            <span className="text-xs text-muted-foreground">
              {groupBy === "task" && protocolView
                ? `${renderedCategoryCount} 类协议能力 · 匹配 ${renderedModelCount} / 总计 ${flatModels.length} 个模型条目 · ${distinctBackendCount} 个 backend`
                : `匹配 ${renderedModelCount} / 总计 ${flatModels.length} 个模型条目 · ${distinctBackendCount} 个 backend`}
            </span>
          </div>
        </div>

        {overviewLoading ? (
          <div className={NOTE_CLASS}>加载 backend 列表…</div>
        ) : overviewError ? (
          <div className="flex items-center gap-2 px-4 py-3 text-xs text-status-danger">
            加载失败：{(overviewErr as Error)?.message ?? "未知错误"}
            <button className={RETRY_BTN_CLASS} onClick={() => refetchOverview()}>
              重试
            </button>
          </div>
        ) : backendRefs.length === 0 &&
          (instancesData?.instances ?? []).length === 0 &&
          groupBy !== "task" ? (
          // v0.14.11 · 0 backend + 非 task 分组: 沿用旧空态; task 分组下走协议卡视图。
          // v0.14.12 · 同时有 env-only instances 时, 这里不再显示空态。
          <>
            {primaryToolbar}
            <div className={EMPTY_STATE_CLASS}>
              <Icon name="layers" size={28} className="opacity-30" />
              <div>尚无项目注册 ML Backend</div>
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
                // 移除无效目录条件 = 回落默认枚举；encode 会把非法参数从 URL
                // 清掉（默认值删除键），issue 随之消失（plan §5 可见提示 + 移除入口）。
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

            {/* 探测失败的 backend 降级提示 (能力目录可能缺条目). */}
            {results.some((r) => r.isError) && (
              <div className="mx-4 mt-3 flex items-center gap-2 rounded-md border border-amber-500/30 bg-status-caution-soft px-3 py-2 text-xs text-foreground">
                <Icon name="warning" size={13} />
                <span>
                  {results.filter((r) => r.isError).length} 个 backend
                  能力探测失败，其条目暂缺；点「刷新」可重探。
                </span>
              </div>
            )}

            {anyCapLoading && flatModels.length === 0 && groupBy !== "task" ? (
              <div className={NOTE_CLASS}>探测各 backend 能力中…</div>
            ) : groupBy === "task" && protocolView ? (
              // v0.14.11 · 协议卡视图: 即使模型清单为空也展示协议卡 (零接入引导)。
              <>
                {/* v0.14.11 · 横幅触发改为「所有协议卡都没有 model 挂载」(覆盖
                    env-only + 注册两条路径), 而非只看注册数。这样 docker-compose
                    自带的 gsam2 / sam3 在跑时就不会再误显示「未接入」横幅。 */}
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
