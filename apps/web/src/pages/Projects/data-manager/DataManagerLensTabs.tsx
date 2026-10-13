import type { ReactNode } from "react";

import type { DataManagerEntityScope } from "@/api/taskViews";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";

const LABELS: Record<DataManagerEntityScope, string> = {
  tasks: "任务",
  objects: "对象",
  tracks: "轨迹",
};

export function DataManagerLensTabs({
  scope,
  availableScopes,
  onScopeChange,
  children,
}: {
  scope: DataManagerEntityScope;
  availableScopes: DataManagerEntityScope[];
  onScopeChange: (scope: DataManagerEntityScope) => void;
  children: ReactNode;
}) {
  return (
    <Tabs
      value={scope}
      onValueChange={(value) => onScopeChange(value as DataManagerEntityScope)}
      className="flex flex-col"
    >
      <TabsList variant="line" aria-label="Data Manager 数据粒度" className="shrink-0">
        {availableScopes.map((item) => (
          <TabsTrigger key={item} value={item}>
            {LABELS[item]}
          </TabsTrigger>
        ))}
      </TabsList>
      <TabsContent value={scope} className="outline-none">
        {children}
      </TabsContent>
    </Tabs>
  );
}
