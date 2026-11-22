import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { usePermissions } from "@/hooks/usePermissions";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { Icon } from "@/components/ui/Icon";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { PageContainer } from "@/components/layout/PageContainer";

import {
  MARKET_TAB_DEFAULTS,
  marketTabCodec,
  migrateLegacyCatalogUrl,
  type MarketTab,
} from "./marketUrlState";
import { RegisteredBackendsTab } from "./RegisteredBackendsTab";
import { RuntimeObservePanel } from "./RuntimeObservePanel";
import { ProtocolCatalogPanel } from "./ProtocolCatalogPanel";
import { ModelDirectoryPanel } from "./ModelDirectoryPanel";

const TABS: { key: MarketTab; label: string; icon: "layers" | "activity" | "bot" }[] = [
  { key: "models", label: "模型目录", icon: "bot" },
  { key: "protocol", label: "协议能力", icon: "layers" },
  { key: "runtime", label: "运行时观测", icon: "activity" },
  { key: "registry", label: "注册管理", icon: "bot" },
];

export function ModelMarketPage() {
  const [params, setParams] = useSearchParams();
  const [migrationNotice] = useState(
    () =>
      params.get("tab") === "catalog" &&
      Array.from(params.keys()).some((key) =>
        [
          "catalog_group",
          "catalog_view",
          "catalog_modality",
          "catalog_family",
          "catalog_infra",
        ].includes(key),
      ),
  );
  const navigate = useNavigate();
  const { role } = usePermissions();
  const isSuperAdmin = role === "super_admin";
  const { state, patch } = useUrlFilterState({
    codec: marketTabCodec,
    defaults: MARKET_TAB_DEFAULTS,
  });
  const activeTab: MarketTab = !isSuperAdmin && state.tab === "runtime" ? "models" : state.tab;
  const rawTab = params.get("tab");
  useEffect(() => {
    if (rawTab === "catalog") setParams(migrateLegacyCatalogUrl(params), { replace: true });
  }, [rawTab, params, setParams]);

  useEffect(() => {
    if (rawTab === "failed") {
      navigate("/ai-pre/jobs?status=failed", { replace: true });
    }
  }, [rawTab, navigate]);

  useEffect(() => {
    if (rawTab !== null && rawTab !== "failed" && rawTab !== "catalog" && rawTab !== activeTab) {
      patch({ tab: activeTab });
    }
  }, [rawTab, activeTab, patch]);

  return (
    <PageContainer>
      <div className="mb-3">
        <h1 className="mb-1 text-xl font-semibold">模型市场</h1>
        <p className="text-sm text-muted-foreground">
          查找模型、了解协议能力，或管理模型服务与运行状态。
        </p>
      </div>

      {migrationNotice && (
        <p role="status" className="mb-3 text-xs text-muted-foreground">
          能力目录已拆分为协议能力和模型目录；原分组及卡片/列表显示设置已移除。
        </p>
      )}
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
        <TabsContent value="models">
          <ModelDirectoryPanel />
        </TabsContent>
        <TabsContent value="protocol">
          <ProtocolCatalogPanel />
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
