/**
 * marketUrlState 单测（plan §5 键表）。
 *
 * 覆盖：缺失默认值、枚举回退 + issue、codec 只写自身键、清除条件保留文本
 * 搜索、对象定位键与问题筛选键互不覆盖、默认值不写入 URL。
 */
import { describe, expect, it } from "vitest";

import {
  MARKET_URL_KEYS,
  MARKET_TAB_DEFAULTS,
  CATALOG_URL_DEFAULTS,
  REGISTRY_URL_DEFAULTS,
  catalogUrlCodec,
  parseCatalogUrlWithIssues,
  focusPatchFor,
  instancesForPoolPatch,
  issueResetPatch,
  marketTabCodec,
  parseMarketTabUrlWithIssues,
  parseRegistryUrlWithIssues,
  registryUrlCodec,
} from "./marketUrlState";

describe("marketTabCodec", () => {
  it("缺失 tab → 默认 catalog，无 issue", () => {
    const { state, issues } = parseMarketTabUrlWithIssues("");
    expect(state).toEqual(MARKET_TAB_DEFAULTS);
    expect(issues).toEqual([]);
  });

  it("未知 tab → 回退 catalog 并给出 issue（调用方负责规范化 URL）", () => {
    const { state, issues } = parseMarketTabUrlWithIssues("?tab=bogus");
    expect(state.tab).toBe("catalog");
    expect(issues).toEqual([{ key: "tab", message: "未知的模型市场视图" }]);
  });

  it("合法枚举原样解析", () => {
    for (const tab of ["catalog", "runtime", "registry"] as const) {
      const { state, issues } = parseMarketTabUrlWithIssues(`?tab=${tab}`);
      expect(state.tab).toBe(tab);
      expect(issues).toEqual([]);
    }
  });

  it("encode 只写 tab 键，默认值删除该键，其它键保留", () => {
    const current = new URLSearchParams("tab=registry&registry_view=instances&foo=1");
    const next = marketTabCodec.encode(current, { tab: "catalog" });
    expect(next.get(MARKET_URL_KEYS.tab)).toBeNull();
    // registry_view 与外部键不被触碰。
    expect(next.get(MARKET_URL_KEYS.registryView)).toBe("instances");
    expect(next.get("foo")).toBe("1");
  });
});

describe("registryUrlCodec.parse", () => {
  it("全部缺失 → 默认值", () => {
    const { state, issues } = parseRegistryUrlWithIssues("");
    expect(state).toEqual(REGISTRY_URL_DEFAULTS);
    expect(issues).toEqual([]);
  });

  it("合法深链完整解析", () => {
    const search =
      "?registry_view=instances&instance_pool=pool-1&instance_health=degraded&issue_severity=critical&binding_view=by-pool&pool_q=sam&instance_id=bk-1";
    const { state, issues } = parseRegistryUrlWithIssues(search);
    expect(state.registryView).toBe("instances");
    expect(state.instancePool).toBe("pool-1");
    expect(state.instanceHealth).toBe("degraded");
    expect(state.issueSeverity).toBe("critical");
    expect(state.bindingView).toBe("by-pool");
    expect(state.poolQ).toBe("sam");
    expect(state.instanceId).toBe("bk-1");
    expect(issues).toEqual([]);
  });

  it("未知子视图 / 非法枚举 → 默认值 + 可见 issue", () => {
    const search =
      "?registry_view=nope&pool_health=terrible&issue_severity=high&binding_view=sideways";
    const { state, issues } = parseRegistryUrlWithIssues(search);
    expect(state.registryView).toBe("pools");
    expect(state.poolHealth).toBe("all");
    expect(state.issueSeverity).toBe("all");
    expect(state.bindingView).toBe("by-project");
    expect(issues.map((i) => i.key)).toEqual([
      MARKET_URL_KEYS.registryView,
      MARKET_URL_KEYS.poolHealth,
      MARKET_URL_KEYS.issueSeverity,
      MARKET_URL_KEYS.bindingView,
    ]);
  });

  it.each(["poolQ", "instanceQ", "gpuQ", "projectQ", "issueQ"] as const)(
    "%s 保留输入空白",
    (key) => {
      const encoded = registryUrlCodec.encode(new URLSearchParams(), {
        ...REGISTRY_URL_DEFAULTS,
        [key]: " SAM 2 ",
      });
      expect(parseRegistryUrlWithIssues(encoded).state[key]).toBe(" SAM 2 ");
    },
  );
});

describe("registryUrlCodec.encode", () => {
  it("默认值不写入 URL；非默认值写入", () => {
    const next = registryUrlCodec.encode(new URLSearchParams(), {
      ...REGISTRY_URL_DEFAULTS,
      registryView: "gpu",
      poolQ: "sam",
    });
    expect(next.get(MARKET_URL_KEYS.registryView)).toBe("gpu");
    expect(next.get(MARKET_URL_KEYS.poolQ)).toBe("sam");
    expect(next.get(MARKET_URL_KEYS.poolHealth)).toBeNull();
  });

  it("只写自身键：tab 与外部键不受影响", () => {
    const current = new URLSearchParams("tab=runtime&foo=bar");
    const next = registryUrlCodec.encode(current, { ...REGISTRY_URL_DEFAULTS, poolQ: "x" });
    expect(next.get("tab")).toBe("runtime");
    expect(next.get("foo")).toBe("bar");
    expect(next.get(MARKET_URL_KEYS.poolQ)).toBe("x");
  });

  it("来回编码是幂等的", () => {
    const first = registryUrlCodec.encode(new URLSearchParams(), {
      ...REGISTRY_URL_DEFAULTS,
      registryView: "issues",
      issueSeverity: "warning",
    });
    const second = registryUrlCodec.encode(first, {
      ...REGISTRY_URL_DEFAULTS,
      registryView: "issues",
      issueSeverity: "warning",
    });
    expect(second.toString()).toBe(first.toString());
  });
});

describe("registryUrlCodec.clear", () => {
  it("清除枚举筛选与池限定，保留文本搜索与定位键（plan §5 键表）", () => {
    const current = new URLSearchParams(
      "?pool_q=sam&pool_health=offline&instance_q=bk&instance_health=offline&instance_pool=p1&binding_pool=p2&issue_q=circuit&issue_severity=warning&issue_code=circuit_open&instance_id=bk-9",
    );
    const next = registryUrlCodec.clear?.(current, REGISTRY_URL_DEFAULTS);
    if (!next) throw new Error("clear must be implemented");
    // 保留
    expect(next.get(MARKET_URL_KEYS.poolQ)).toBe("sam");
    expect(next.get(MARKET_URL_KEYS.instanceQ)).toBe("bk");
    expect(next.get(MARKET_URL_KEYS.issueQ)).toBe("circuit");
    expect(next.get(MARKET_URL_KEYS.instanceId)).toBe("bk-9");
    // 清除
    expect(next.get(MARKET_URL_KEYS.poolHealth)).toBeNull();
    expect(next.get(MARKET_URL_KEYS.instanceHealth)).toBeNull();
    expect(next.get(MARKET_URL_KEYS.instancePool)).toBeNull();
    expect(next.get(MARKET_URL_KEYS.bindingPool)).toBeNull();
    expect(next.get(MARKET_URL_KEYS.issueSeverity)).toBeNull();
    expect(next.get(MARKET_URL_KEYS.issueCode)).toBeNull();
  });
});

// ── 能力目录 codec（plan §5 catalog_* 键表） ────────────────────────────────

describe("catalogUrlCodec.parse", () => {
  it("搜索往返保留输入空白", () => {
    const encoded = catalogUrlCodec.encode(new URLSearchParams(), {
      ...CATALOG_URL_DEFAULTS,
      catalogQ: " SAM 2 ",
    });
    expect(parseCatalogUrlWithIssues(encoded).state.catalogQ).toBe(" SAM 2 ");
  });
  it("全部缺失 → 默认值（task 分组 / 卡片视图）", () => {
    const { state, issues } = parseCatalogUrlWithIssues("");
    expect(state).toEqual(CATALOG_URL_DEFAULTS);
    expect(issues).toEqual([]);
  });

  it("多选轴使用重复键解析", () => {
    const search = "?catalog_task=detection&catalog_task=ocr&catalog_modality=image";
    const { state } = parseCatalogUrlWithIssues(search);
    expect(state.catalogTask).toEqual(["detection", "ocr"]);
    expect(state.catalogModality).toEqual(["image"]);
  });

  it("未知视图 / 分组 → 默认值 + 可见 issue", () => {
    const search = "?catalog_view=table&catalog_group=color";
    const { state, issues } = parseCatalogUrlWithIssues(search);
    expect(state.catalogView).toBe("cards");
    expect(state.catalogGroup).toBe("task");
    expect(issues.map((i) => i.key)).toEqual([
      MARKET_URL_KEYS.catalogView,
      MARKET_URL_KEYS.catalogGroup,
    ]);
  });
});

describe("catalogUrlCodec.encode", () => {
  it("多选轴写为重复键，空数组删除该键", () => {
    const first = catalogUrlCodec.encode(new URLSearchParams(), {
      ...CATALOG_URL_DEFAULTS,
      catalogTask: ["detection", "ocr"],
    });
    expect(first.getAll(MARKET_URL_KEYS.catalogTask)).toEqual(["detection", "ocr"]);
    const second = catalogUrlCodec.encode(first, {
      ...CATALOG_URL_DEFAULTS,
      catalogTask: [],
    });
    expect(second.get(MARKET_URL_KEYS.catalogTask)).toBeNull();
  });

  it("只写自身键：registry_view 等其它键保留", () => {
    const current = new URLSearchParams("registry_view=instances&tab=registry");
    const next = catalogUrlCodec.encode(current, {
      ...CATALOG_URL_DEFAULTS,
      catalogQ: "sam",
    });
    expect(next.get(MARKET_URL_KEYS.registryView)).toBe("instances");
    expect(next.get(MARKET_URL_KEYS.catalogQ)).toBe("sam");
  });
});

describe("catalogUrlCodec.clear", () => {
  it("清除条件只清多选轴，保留搜索 / 分组 / 显示方式（plan §5）", () => {
    const current = new URLSearchParams(
      "?catalog_q=sam&catalog_group=backend&catalog_view=list&catalog_task=detection&catalog_family=sam",
    );
    const next = catalogUrlCodec.clear?.(current, CATALOG_URL_DEFAULTS);
    if (!next) throw new Error("clear must be implemented");
    expect(next.get(MARKET_URL_KEYS.catalogQ)).toBe("sam");
    expect(next.get(MARKET_URL_KEYS.catalogGroup)).toBe("backend");
    expect(next.get(MARKET_URL_KEYS.catalogView)).toBe("list");
    expect(next.get(MARKET_URL_KEYS.catalogTask)).toBeNull();
    expect(next.get(MARKET_URL_KEYS.catalogFamily)).toBeNull();
  });
});

describe("导航辅助", () => {
  it("focusPatchFor 按目标子视图填充定位键并清空互斥键", () => {
    expect(focusPatchFor("instance", "bk-1")).toEqual({
      registryView: "instances",
      instanceId: "bk-1",
      gpuId: "",
      poolId: "",
      instancePool: "",
      instanceQ: "",
      instanceHealth: "all",
    });
    expect(focusPatchFor("gpu", "node-a/index:0").registryView).toBe("gpu");
    expect(focusPatchFor("pool", "pool-1").registryView).toBe("pools");
  });

  it("instancesForPoolPatch 切到实例子视图并带池定位条件", () => {
    expect(instancesForPoolPatch("pool-1")).toEqual({
      registryView: "instances",
      instancePool: "pool-1",
      instanceId: "",
      gpuId: "",
      poolId: "",
      instanceQ: "",
      instanceHealth: "all",
    });
  });

  it.each(["pool", "gpu", "instance"] as const)(
    "定位 %s 清除目标页冲突，保留问题上下文",
    (kind) => {
      const previous = {
        ...REGISTRY_URL_DEFAULTS,
        poolQ: "other-pool",
        poolHealth: "healthy" as const,
        gpuQ: "other-gpu",
        instanceQ: "other-instance",
        instanceHealth: "offline" as const,
        issueCode: "pool_offline",
        issueSeverity: "critical" as const,
      };
      const next = registryUrlCodec.encode(new URLSearchParams("foreign=keep"), {
        ...previous,
        ...focusPatchFor(kind, "target"),
      });
      const { state } = registryUrlCodec.parse(next);
      expect(state.issueCode).toBe(previous.issueCode);
      expect(state.issueSeverity).toBe(previous.issueSeverity);
      expect(next.get("foreign")).toBe("keep");
      expect(state.poolQ).toBe(kind === "pool" ? "" : previous.poolQ);
      expect(state.poolHealth).toBe(kind === "pool" ? "all" : previous.poolHealth);
      expect(state.gpuQ).toBe(kind === "gpu" ? "" : previous.gpuQ);
      expect(state.instanceQ).toBe(kind === "instance" ? "" : previous.instanceQ);
      expect(state.instanceHealth).toBe(kind === "instance" ? "all" : previous.instanceHealth);
    },
  );

  it("issueResetPatch 只映射注册子视图的枚举键", () => {
    expect(issueResetPatch(MARKET_URL_KEYS.poolHealth)).toEqual({ poolHealth: "all" });
    expect(issueResetPatch(MARKET_URL_KEYS.bindingView)).toEqual({ bindingView: "by-project" });
    expect(issueResetPatch("tab")).toBeNull();
  });
});
