/**
 * 模型市场页面局部 URL 状态 codec（plan §5）。
 *
 * 两个 codec 各只写自己的键，互不覆盖：
 *   - `marketTabCodec`   → `tab=catalog|runtime|registry`（主 TAB）。
 *   - `registryUrlCodec` → 注册子 TAB（`registry_view`）、五个子视图各自的
 *     筛选/搜索键，以及对象定位键 `instance_id` / `gpu_id` / `pool_id`。
 *
 * 约定：
 *   - 缺失即默认值；encode 时默认值不写入 URL（保持深链简洁）。
 *   - 未知主 TAB / 子 TAB 枚举回退默认页并返回 issue（调用方负责规范化 URL）。
 *   - 筛选枚举非法时回退默认值并返回 issue（调用方显示可见提示与移除入口，
 *     不静默扩大结果范围）。
 *   - 问题筛选键（issue_*）与对象定位键（*_id）分开，不相互覆盖。
 *   - 文本搜索条件（*_q）在“清除条件”时保留（plan §5 键表）。
 */
import type { UrlStateCodec, UrlStateIssue } from "@/hooks/useUrlFilterState";

// ── 枚举 ─────────────────────────────────────────────────────────────────────

export type MarketTab = "catalog" | "runtime" | "registry";
export const MARKET_TAB_VALUES = ["catalog", "runtime", "registry"] as const;

export type RegistryView = "pools" | "instances" | "gpu" | "projects" | "issues";
export const REGISTRY_VIEW_VALUES = ["pools", "instances", "gpu", "projects", "issues"] as const;

/** 项目管理员可见的注册子视图（服务端裁剪后的只读集合）。 */
export const PROJECT_ADMIN_REGISTRY_VIEWS: readonly RegistryView[] = ["pools", "instances"];

export type RegistryHealthFilter = "all" | "healthy" | "degraded" | "offline" | "unknown";
export const REGISTRY_HEALTH_VALUES = ["all", "healthy", "degraded", "offline", "unknown"] as const;

export type IssueSeverityFilter = "all" | "blocker" | "critical" | "warning" | "info";
export const ISSUE_SEVERITY_VALUES = ["all", "blocker", "critical", "warning", "info"] as const;

export type BindingView = "by-project" | "by-pool";
export const BINDING_VIEW_VALUES = ["by-project", "by-pool"] as const;

// ── URL 键名 ─────────────────────────────────────────────────────────────────

export const MARKET_URL_KEYS = {
  tab: "tab",
  registryView: "registry_view",
  poolQ: "pool_q",
  poolHealth: "pool_health",
  instanceQ: "instance_q",
  instanceHealth: "instance_health",
  instancePool: "instance_pool",
  instanceId: "instance_id",
  poolId: "pool_id",
  gpuQ: "gpu_q",
  gpuId: "gpu_id",
  projectQ: "project_q",
  bindingView: "binding_view",
  bindingPool: "binding_pool",
  issueQ: "issue_q",
  issueSeverity: "issue_severity",
  issuePool: "issue_pool",
  issueInstance: "issue_instance",
  issueGpu: "issue_gpu",
  issueCode: "issue_code",
  catalogQ: "catalog_q",
  catalogGroup: "catalog_group",
  catalogView: "catalog_view",
  catalogTask: "catalog_task",
  catalogModality: "catalog_modality",
  catalogFamily: "catalog_family",
  catalogInfra: "catalog_infra",
} as const;

// ── 主 TAB codec ─────────────────────────────────────────────────────────────

export interface MarketTabUrlState {
  tab: MarketTab;
}

export const MARKET_TAB_DEFAULTS: MarketTabUrlState = { tab: "catalog" };

function isMarketTab(value: string): value is MarketTab {
  return (MARKET_TAB_VALUES as readonly string[]).includes(value);
}

export function parseMarketTabUrlWithIssues(search: string | URLSearchParams): {
  state: MarketTabUrlState;
  issues: UrlStateIssue[];
} {
  const params = new URLSearchParams(typeof search === "string" ? search : search.toString());
  const issues: UrlStateIssue[] = [];
  const raw = params.get(MARKET_URL_KEYS.tab)?.trim() ?? "";
  if (raw && !isMarketTab(raw)) {
    issues.push({ key: MARKET_URL_KEYS.tab, message: "未知的模型市场视图" });
  }
  return { state: { tab: isMarketTab(raw) ? raw : "catalog" }, issues };
}

export const marketTabCodec: UrlStateCodec<MarketTabUrlState> = {
  parse: parseMarketTabUrlWithIssues,
  encode: (current, state) => {
    const next = new URLSearchParams(current);
    if (state.tab === MARKET_TAB_DEFAULTS.tab) next.delete(MARKET_URL_KEYS.tab);
    else next.set(MARKET_URL_KEYS.tab, state.tab);
    return next;
  },
};

// ── 注册管理 codec ───────────────────────────────────────────────────────────

export interface RegistryUrlState {
  registryView: RegistryView;
  /** 服务池子视图：搜索池名/池 ID/策略/成员名与成员 ID。 */
  poolQ: string;
  poolHealth: RegistryHealthFilter;
  /** 实例子视图：搜索名称/ID/URL/来源项目。 */
  instanceQ: string;
  instanceHealth: RegistryHealthFilter;
  /** 实例子视图的池定位条件（来自服务池“查看实例”跳转）。 */
  instancePool: string;
  /** 实例详情定位键（深链直接打开详情 Sheet）。 */
  instanceId: string;
  /** 服务池行展开定位键。 */
  poolId: string;
  /** GPU 子视图：搜索资源 ID/节点/设备令牌。 */
  gpuQ: string;
  /** GPU 行展开定位键。 */
  gpuId: string;
  /** 项目绑定子视图：按项目视图搜项目，按服务池视图搜池。 */
  projectQ: string;
  bindingView: BindingView;
  /** 项目绑定子视图的池限定条件，可单独移除。 */
  bindingPool: string;
  /** 问题中心：code 文本搜索（独立于精确 code 筛选）。 */
  issueQ: string;
  issueSeverity: IssueSeverityFilter;
  issuePool: string;
  issueInstance: string;
  issueGpu: string;
  issueCode: string;
}

export const REGISTRY_URL_DEFAULTS: RegistryUrlState = {
  registryView: "pools",
  poolQ: "",
  poolHealth: "all",
  instanceQ: "",
  instanceHealth: "all",
  instancePool: "",
  instanceId: "",
  poolId: "",
  gpuQ: "",
  gpuId: "",
  projectQ: "",
  bindingView: "by-project",
  bindingPool: "",
  issueQ: "",
  issueSeverity: "all",
  issuePool: "",
  issueInstance: "",
  issueGpu: "",
  issueCode: "",
};

function isRegistryView(value: string): value is RegistryView {
  return (REGISTRY_VIEW_VALUES as readonly string[]).includes(value);
}

function isHealthFilter(value: string): value is RegistryHealthFilter {
  return (REGISTRY_HEALTH_VALUES as readonly string[]).includes(value);
}

function isSeverityFilter(value: string): value is IssueSeverityFilter {
  return (ISSUE_SEVERITY_VALUES as readonly string[]).includes(value);
}

function isBindingView(value: string): value is BindingView {
  return (BINDING_VIEW_VALUES as readonly string[]).includes(value);
}

export function parseRegistryUrlWithIssues(search: string | URLSearchParams): {
  state: RegistryUrlState;
  issues: UrlStateIssue[];
} {
  const params = new URLSearchParams(typeof search === "string" ? search : search.toString());
  const issues: UrlStateIssue[] = [];
  const rawView = params.get(MARKET_URL_KEYS.registryView)?.trim() ?? "";
  if (rawView && !isRegistryView(rawView)) {
    issues.push({ key: MARKET_URL_KEYS.registryView, message: "未知的注册子视图" });
  }
  const rawPoolHealth = params.get(MARKET_URL_KEYS.poolHealth)?.trim() ?? "";
  if (rawPoolHealth && !isHealthFilter(rawPoolHealth)) {
    issues.push({ key: MARKET_URL_KEYS.poolHealth, message: "无效的服务池健康筛选" });
  }
  const rawInstanceHealth = params.get(MARKET_URL_KEYS.instanceHealth)?.trim() ?? "";
  if (rawInstanceHealth && !isHealthFilter(rawInstanceHealth)) {
    issues.push({ key: MARKET_URL_KEYS.instanceHealth, message: "无效的实例健康筛选" });
  }
  const rawSeverity = params.get(MARKET_URL_KEYS.issueSeverity)?.trim() ?? "";
  if (rawSeverity && !isSeverityFilter(rawSeverity)) {
    issues.push({ key: MARKET_URL_KEYS.issueSeverity, message: "无效的问题严重度筛选" });
  }
  const rawBindingView = params.get(MARKET_URL_KEYS.bindingView)?.trim() ?? "";
  if (rawBindingView && !isBindingView(rawBindingView)) {
    issues.push({ key: MARKET_URL_KEYS.bindingView, message: "无效的项目绑定视图" });
  }
  return {
    state: {
      registryView: isRegistryView(rawView) ? rawView : "pools",
      poolQ: params.get(MARKET_URL_KEYS.poolQ) ?? "",
      poolHealth: isHealthFilter(rawPoolHealth) ? rawPoolHealth : "all",
      instanceQ: params.get(MARKET_URL_KEYS.instanceQ) ?? "",
      instanceHealth: isHealthFilter(rawInstanceHealth) ? rawInstanceHealth : "all",
      instancePool: params.get(MARKET_URL_KEYS.instancePool)?.trim() ?? "",
      instanceId: params.get(MARKET_URL_KEYS.instanceId)?.trim() ?? "",
      poolId: params.get(MARKET_URL_KEYS.poolId)?.trim() ?? "",
      gpuQ: params.get(MARKET_URL_KEYS.gpuQ) ?? "",
      gpuId: params.get(MARKET_URL_KEYS.gpuId)?.trim() ?? "",
      projectQ: params.get(MARKET_URL_KEYS.projectQ) ?? "",
      bindingView: isBindingView(rawBindingView) ? rawBindingView : "by-project",
      bindingPool: params.get(MARKET_URL_KEYS.bindingPool)?.trim() ?? "",
      issueQ: params.get(MARKET_URL_KEYS.issueQ) ?? "",
      issueSeverity: isSeverityFilter(rawSeverity) ? rawSeverity : "all",
      issuePool: params.get(MARKET_URL_KEYS.issuePool)?.trim() ?? "",
      issueInstance: params.get(MARKET_URL_KEYS.issueInstance)?.trim() ?? "",
      issueGpu: params.get(MARKET_URL_KEYS.issueGpu)?.trim() ?? "",
      issueCode: params.get(MARKET_URL_KEYS.issueCode)?.trim() ?? "",
    },
    issues,
  };
}

/** Encode only the registry-owned keys; `tab` and foreign keys stay untouched. */
function encodeRegistryUrl(current: URLSearchParams, state: RegistryUrlState): URLSearchParams {
  const next = new URLSearchParams(current);
  const setOrDelete = (key: string, value: string, fallback: string) => {
    if (value === fallback) next.delete(key);
    else next.set(key, value);
  };
  setOrDelete(MARKET_URL_KEYS.registryView, state.registryView, REGISTRY_URL_DEFAULTS.registryView);
  setOrDelete(MARKET_URL_KEYS.poolQ, state.poolQ, REGISTRY_URL_DEFAULTS.poolQ);
  setOrDelete(MARKET_URL_KEYS.poolHealth, state.poolHealth, REGISTRY_URL_DEFAULTS.poolHealth);
  setOrDelete(MARKET_URL_KEYS.instanceQ, state.instanceQ, REGISTRY_URL_DEFAULTS.instanceQ);
  setOrDelete(
    MARKET_URL_KEYS.instanceHealth,
    state.instanceHealth,
    REGISTRY_URL_DEFAULTS.instanceHealth,
  );
  setOrDelete(MARKET_URL_KEYS.instancePool, state.instancePool, REGISTRY_URL_DEFAULTS.instancePool);
  setOrDelete(MARKET_URL_KEYS.instanceId, state.instanceId, REGISTRY_URL_DEFAULTS.instanceId);
  setOrDelete(MARKET_URL_KEYS.poolId, state.poolId, REGISTRY_URL_DEFAULTS.poolId);
  setOrDelete(MARKET_URL_KEYS.gpuQ, state.gpuQ, REGISTRY_URL_DEFAULTS.gpuQ);
  setOrDelete(MARKET_URL_KEYS.gpuId, state.gpuId, REGISTRY_URL_DEFAULTS.gpuId);
  setOrDelete(MARKET_URL_KEYS.projectQ, state.projectQ, REGISTRY_URL_DEFAULTS.projectQ);
  setOrDelete(MARKET_URL_KEYS.bindingView, state.bindingView, REGISTRY_URL_DEFAULTS.bindingView);
  setOrDelete(MARKET_URL_KEYS.bindingPool, state.bindingPool, REGISTRY_URL_DEFAULTS.bindingPool);
  setOrDelete(MARKET_URL_KEYS.issueQ, state.issueQ, REGISTRY_URL_DEFAULTS.issueQ);
  setOrDelete(
    MARKET_URL_KEYS.issueSeverity,
    state.issueSeverity,
    REGISTRY_URL_DEFAULTS.issueSeverity,
  );
  setOrDelete(MARKET_URL_KEYS.issuePool, state.issuePool, REGISTRY_URL_DEFAULTS.issuePool);
  setOrDelete(
    MARKET_URL_KEYS.issueInstance,
    state.issueInstance,
    REGISTRY_URL_DEFAULTS.issueInstance,
  );
  setOrDelete(MARKET_URL_KEYS.issueGpu, state.issueGpu, REGISTRY_URL_DEFAULTS.issueGpu);
  setOrDelete(MARKET_URL_KEYS.issueCode, state.issueCode, REGISTRY_URL_DEFAULTS.issueCode);
  return next;
}

/**
 * “清除条件”范围（plan §5 键表）：清掉各子视图的枚举筛选、池限定与问题附着
 * 筛选；文本搜索（*_q）与对象定位键（*_id）保留——搜索是用户输入的上下文，
 * 定位键由详情关闭动作自己负责。
 */
function clearRegistryUrl(current: URLSearchParams, _defaults: RegistryUrlState): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const key of [
    MARKET_URL_KEYS.poolHealth,
    MARKET_URL_KEYS.instanceHealth,
    MARKET_URL_KEYS.instancePool,
    MARKET_URL_KEYS.bindingPool,
    MARKET_URL_KEYS.issueSeverity,
    MARKET_URL_KEYS.issuePool,
    MARKET_URL_KEYS.issueInstance,
    MARKET_URL_KEYS.issueGpu,
    MARKET_URL_KEYS.issueCode,
  ]) {
    next.delete(key);
  }
  return next;
}

export const registryUrlCodec: UrlStateCodec<RegistryUrlState> = {
  parse: parseRegistryUrlWithIssues,
  encode: encodeRegistryUrl,
  clear: clearRegistryUrl,
};

// ── 能力目录 codec ───────────────────────────────────────────────────────────

export type CatalogView = "cards" | "list";
export const CATALOG_VIEW_VALUES = ["cards", "list"] as const;

export type CatalogGroup = "task" | "backend" | "infra" | "none";
export const CATALOG_GROUP_VALUES = ["task", "backend", "infra", "none"] as const;

export interface CatalogUrlState {
  /** 搜索模型名 / ID / 模型族 / 任务 / 来源。 */
  catalogQ: string;
  catalogGroup: CatalogGroup;
  catalogView: CatalogView;
  /** 多选轴使用重复键（plan §5 键表）。 */
  catalogTask: string[];
  catalogModality: string[];
  catalogFamily: string[];
  catalogInfra: string[];
}

export const CATALOG_URL_DEFAULTS: CatalogUrlState = {
  catalogQ: "",
  catalogGroup: "task",
  catalogView: "cards",
  catalogTask: [],
  catalogModality: [],
  catalogFamily: [],
  catalogInfra: [],
};

function isCatalogView(value: string): value is CatalogView {
  return (CATALOG_VIEW_VALUES as readonly string[]).includes(value);
}

function isCatalogGroup(value: string): value is CatalogGroup {
  return (CATALOG_GROUP_VALUES as readonly string[]).includes(value);
}

export function parseCatalogUrlWithIssues(search: string | URLSearchParams): {
  state: CatalogUrlState;
  issues: UrlStateIssue[];
} {
  const params = new URLSearchParams(typeof search === "string" ? search : search.toString());
  const issues: UrlStateIssue[] = [];
  const rawView = params.get(MARKET_URL_KEYS.catalogView)?.trim() ?? "";
  if (rawView && !isCatalogView(rawView)) {
    issues.push({ key: MARKET_URL_KEYS.catalogView, message: "无效的目录显示方式" });
  }
  const rawGroup = params.get(MARKET_URL_KEYS.catalogGroup)?.trim() ?? "";
  if (rawGroup && !isCatalogGroup(rawGroup)) {
    issues.push({ key: MARKET_URL_KEYS.catalogGroup, message: "无效的目录分组" });
  }
  return {
    state: {
      catalogQ: params.get(MARKET_URL_KEYS.catalogQ) ?? "",
      catalogGroup: isCatalogGroup(rawGroup) ? rawGroup : "task",
      catalogView: isCatalogView(rawView) ? rawView : "cards",
      catalogTask: params
        .getAll(MARKET_URL_KEYS.catalogTask)
        .map((v) => v.trim())
        .filter(Boolean),
      catalogModality: params
        .getAll(MARKET_URL_KEYS.catalogModality)
        .map((v) => v.trim())
        .filter(Boolean),
      catalogFamily: params
        .getAll(MARKET_URL_KEYS.catalogFamily)
        .map((v) => v.trim())
        .filter(Boolean),
      catalogInfra: params
        .getAll(MARKET_URL_KEYS.catalogInfra)
        .map((v) => v.trim())
        .filter(Boolean),
    },
    issues,
  };
}

function encodeCatalogUrl(current: URLSearchParams, state: CatalogUrlState): URLSearchParams {
  const next = new URLSearchParams(current);
  const setMulti = (key: string, values: string[]) => {
    next.delete(key);
    for (const value of values) {
      if (value) next.append(key, value);
    }
  };
  setOrDeleteKey(next, MARKET_URL_KEYS.catalogQ, state.catalogQ, CATALOG_URL_DEFAULTS.catalogQ);
  setOrDeleteKey(
    next,
    MARKET_URL_KEYS.catalogGroup,
    state.catalogGroup,
    CATALOG_URL_DEFAULTS.catalogGroup,
  );
  setOrDeleteKey(
    next,
    MARKET_URL_KEYS.catalogView,
    state.catalogView,
    CATALOG_URL_DEFAULTS.catalogView,
  );
  setMulti(MARKET_URL_KEYS.catalogTask, state.catalogTask);
  setMulti(MARKET_URL_KEYS.catalogModality, state.catalogModality);
  setMulti(MARKET_URL_KEYS.catalogFamily, state.catalogFamily);
  setMulti(MARKET_URL_KEYS.catalogInfra, state.catalogInfra);
  return next;
}

function setOrDeleteKey(
  params: URLSearchParams,
  key: string,
  value: string,
  fallback: string,
): void {
  if (value === fallback) params.delete(key);
  else params.set(key, value);
}

/**
 * 目录「清除条件」只清多选轴；搜索、分组、显示方式保留（plan §5 键表）。
 */
function clearCatalogUrl(current: URLSearchParams, _defaults: CatalogUrlState): URLSearchParams {
  const next = new URLSearchParams(current);
  for (const key of [
    MARKET_URL_KEYS.catalogTask,
    MARKET_URL_KEYS.catalogModality,
    MARKET_URL_KEYS.catalogFamily,
    MARKET_URL_KEYS.catalogInfra,
  ]) {
    next.delete(key);
  }
  return next;
}

export const catalogUrlCodec: UrlStateCodec<CatalogUrlState> = {
  parse: parseCatalogUrlWithIssues,
  encode: encodeCatalogUrl,
  clear: clearCatalogUrl,
};

/** 目录子视图自己的 URL 键（issue 提示归属）。 */
export const CATALOG_URL_ISSUE_KEYS: readonly string[] = [
  MARKET_URL_KEYS.catalogView,
  MARKET_URL_KEYS.catalogGroup,
];

// ── 导航辅助 ─────────────────────────────────────────────────────────────────

/**
 * 构造“跳到目标子视图并定位一个对象”的 patch（plan §5：问题影响对象通过目标
 * 子页的定位键进入具体对象）。互斥键会被清空，避免旧定位残留。
 */
export function focusPatchFor(
  kind: "instance" | "gpu" | "pool",
  id: string,
): Partial<RegistryUrlState> {
  return {
    registryView: kind === "instance" ? "instances" : kind === "gpu" ? "gpu" : "pools",
    instanceId: kind === "instance" ? id : "",
    gpuId: kind === "gpu" ? id : "",
    poolId: kind === "pool" ? id : "",
    instancePool: "",
    // Clear only destination filters that could hide the requested object.
    ...(kind === "instance" ? { instanceQ: "", instanceHealth: "all" as const } : {}),
    ...(kind === "gpu" ? { gpuQ: "" } : {}),
    ...(kind === "pool" ? { poolQ: "", poolHealth: "all" as const } : {}),
  };
}

/**
 * 服务池 → 实例 的跳转 patch：切到实例子视图并按池过滤（plan §5
 * `registry_view=instances&instance_pool=<id>`，替代旧的 registry:focus-tab
 * 自定义事件，刷新/前进后退可恢复）。
 */
export function instancesForPoolPatch(poolId: string): Partial<RegistryUrlState> {
  return { ...focusPatchFor("instance", ""), instancePool: poolId };
}

/** 每个 issue 键回到默认值所需的 patch，供“移除无效条件”入口使用。 */
export function issueResetPatch(key: string): Partial<RegistryUrlState> | null {
  switch (key) {
    case MARKET_URL_KEYS.registryView:
      return { registryView: REGISTRY_URL_DEFAULTS.registryView };
    case MARKET_URL_KEYS.poolHealth:
      return { poolHealth: "all" };
    case MARKET_URL_KEYS.instanceHealth:
      return { instanceHealth: "all" };
    case MARKET_URL_KEYS.issueSeverity:
      return { issueSeverity: "all" };
    case MARKET_URL_KEYS.bindingView:
      return { bindingView: "by-project" };
    default:
      return null;
  }
}

/** 各子视图自己的 URL 键；issue 提示只在其所属子视图显示。 */
export const REGISTRY_VIEW_URL_KEYS: Record<RegistryView, readonly string[]> = {
  pools: [MARKET_URL_KEYS.poolQ, MARKET_URL_KEYS.poolHealth, MARKET_URL_KEYS.poolId],
  instances: [
    MARKET_URL_KEYS.instanceQ,
    MARKET_URL_KEYS.instanceHealth,
    MARKET_URL_KEYS.instancePool,
    MARKET_URL_KEYS.instanceId,
  ],
  gpu: [MARKET_URL_KEYS.gpuQ, MARKET_URL_KEYS.gpuId],
  projects: [MARKET_URL_KEYS.projectQ, MARKET_URL_KEYS.bindingView, MARKET_URL_KEYS.bindingPool],
  issues: [
    MARKET_URL_KEYS.issueQ,
    MARKET_URL_KEYS.issueSeverity,
    MARKET_URL_KEYS.issuePool,
    MARKET_URL_KEYS.issueInstance,
    MARKET_URL_KEYS.issueGpu,
    MARKET_URL_KEYS.issueCode,
  ],
};

/** 过滤出某个子视图需要展示的 URL issue（plan §5：非法枚举在所属视图提示）。 */
export function urlIssuesForView(issues: UrlStateIssue[], view: RegistryView): UrlStateIssue[] {
  const owned = REGISTRY_VIEW_URL_KEYS[view];
  return issues.filter((issue) => owned.includes(issue.key));
}
