import { useEffect } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { usePermissions } from "@/hooks/usePermissions";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { Icon } from "@/components/ui/Icon";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { PageContainer } from "@/components/layout/PageContainer";

import { MARKET_TAB_DEFAULTS, marketTabCodec, type MarketTab } from "./marketUrlState";
import { RegisteredBackendsTab } from "./RegisteredBackendsTab";
import { RuntimeObservePanel } from "./RuntimeObservePanel";
import { CapabilityCatalogPanel } from "./CapabilityCatalogPanel";

// v0.9.12 BUG B-14 · 删 failed tab; 失败预测已迁到 /ai-pre/jobs?status=failed.
// v0.23.4 P4 · FailedPredictionsTab.tsx + ObserveBackendsPanel.tsx 已删除
// （审计确认无任何 import；旧注释提及的引用已不存在）。
//
// 页面骨架（plan §3）：一个 H1 → 主 TAB（line 变体）→ 当前主 Panel。父页面
// 不再放全局统计卡——项目数留在项目绑定、模型数留在能力目录、运行统计留在
// 运行时观测，避免同名统计的第二套口径。
const TABS: { key: MarketTab; label: string; icon: "layers" | "activity" | "bot" }[] = [
  { key: "catalog", label: "能力目录", icon: "layers" },
  { key: "runtime", label: "运行时观测", icon: "activity" },
  { key: "registry", label: "注册管理", icon: "bot" },
];

export function ModelMarketPage() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  // 页面对 super_admin + project_admin 开放, 但「运行时观测」tab 走的 /observe
  // 是 super_admin only。故 project_admin 只看能力目录 + (只读) 注册管理:
  // 运行时观测隐藏, 不发相应超管请求 (否则每 60s 打一次 403)。
  const { role } = usePermissions();
  const isSuperAdmin = role === "super_admin";
  // 主 TAB 写入 URL（plan §5）：显式切换新增浏览器历史，深链/刷新可恢复。
  const { state, patch } = useUrlFilterState({
    codec: marketTabCodec,
    defaults: MARKET_TAB_DEFAULTS,
  });
  // 非超管深链到 ?tab=runtime → 回落到能力目录 (该 tab 对其不可见)。
  const activeTab: MarketTab = !isSuperAdmin && state.tab === "runtime" ? "catalog" : state.tab;
  const rawTab = params.get("tab");

  // 兼容老书签: ?tab=failed → 自动 redirect 到 /ai-pre/jobs?status=failed
  useEffect(() => {
    if (rawTab === "failed") {
      navigate("/ai-pre/jobs?status=failed", { replace: true });
    }
  }, [rawTab, navigate]);

  // 未知枚举 / 角色不可见的 TAB：回退到允许的默认页并规范化 URL（plan §5）。
  // 仅当 URL 里真的带着 tab 键时才写回，避免把干净的 /model-market 改写；
  // ?tab=failed 交给上面的重定向 effect 处理，两者不能同时写路由。
  useEffect(() => {
    if (rawTab !== null && rawTab !== "failed" && rawTab !== activeTab) {
      patch({ tab: activeTab });
    }
  }, [rawTab, activeTab, patch]);

  return (
    <PageContainer>
      <div className="mb-3">
        <h1 className="mb-1 text-xl font-semibold">模型市场</h1>
        <p className="text-sm text-muted-foreground">
          浏览模型能力、查看运行状态和管理后端服务。
          {/* v0.10.38 · 视频追踪任务监控已迁至 /ai-pre/jobs 视频 tab (epic 阶段 3) */}
        </p>
      </div>

      <Tabs
        value={activeTab}
        activationMode="manual"
        onValueChange={(value) => patch({ tab: value as MarketTab }, { replace: false })}
      >
        <TabsList
          variant="line"
          aria-label="模型市场视图"
          className="mb-3 gap-4 max-md:mb-3 max-md:w-full max-md:gap-2 max-md:overflow-x-auto max-md:pb-1.5"
        >
          {TABS.filter((tab) => isSuperAdmin || tab.key !== "runtime").map((tab) => (
            <TabsTrigger key={tab.key} value={tab.key} className="max-md:flex-none">
              <Icon name={tab.icon} size={13} />
              {tab.label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="catalog">
          <CapabilityCatalogPanel />
        </TabsContent>
        <TabsContent value="runtime">
          <RuntimeObservePanel />
        </TabsContent>
        <TabsContent value="registry">
          <RegisteredBackendsTab />
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}
