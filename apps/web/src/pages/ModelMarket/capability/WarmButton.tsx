// 模型预热按钮(从 CapabilityCatalogPanel.tsx 拆出,行为零变化)。RuntimeCell 与 ModelCard 共用。

import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { mlBackendsApi } from "@/api/ml-backends";
import type { FlatModel } from "./types";

export function WarmButton({
  item,
  variants,
  compact = false,
  size = "sm",
}: {
  item: FlatModel;
  variants: Record<string, string>;
  compact?: boolean;
  size?: "xs" | "sm" | "md";
}) {
  const qc = useQueryClient();
  const pushToast = useToastStore((s) => s.push);
  const [busy, setBusy] = useState(false);
  const canWarm = Boolean(item.source === "registered" && item.projectId && item.warmupEndpoint);
  const onWarm = async () => {
    if (!canWarm || busy) return;
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        ...(item.model.task ? { task: item.model.task } : {}),
        ...(Object.keys(variants).length > 0 ? { variants } : {}),
      };
      const res = await mlBackendsApi.warmup(item.projectId, item.backendId, body);
      qc.invalidateQueries({ queryKey: ["admin", "ml-integrations", "overview"] });
      qc.invalidateQueries({ queryKey: ["ml-backend-capabilities"] });
      pushToast({
        msg: res.cache_hit ? "模型已加载" : "模型预热完成",
        sub: res.evicted ? `已释放 ${res.evicted}` : undefined,
        kind: "success",
      });
    } catch (err) {
      pushToast({ msg: "预热失败", sub: (err as Error).message, kind: "error" });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button
      size={size}
      onClick={onWarm}
      disabled={!canWarm || busy}
      title={canWarm ? "请求预热所选模型规格" : "服务未声明支持预热，或尚未在项目中启用"}
      className={
        compact ? (size === "xs" ? "size-6 min-w-6 px-0" : "size-8 min-w-8 px-0") : undefined
      }
    >
      <Icon name={busy ? "loader2" : "play"} size={11} className={busy ? "spin" : undefined} />
      {compact ? "" : "预热"}
    </Button>
  );
}
