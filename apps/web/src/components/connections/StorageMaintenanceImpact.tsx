import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type {
  StorageLocationOut,
  StorageMaintenanceImpact as Impact,
} from "@/api/generated/types.gen";
import { apiClient } from "@/api/client";
import { usePermissions } from "@/hooks/usePermissions";
import { useSessionRevision } from "@/hooks/useSessionGuard";
import { useAuthStore } from "@/stores/authStore";
import { verifiedContentError } from "@/hooks/useVerifiedImage";
import { Button } from "@/components/ui/Button";
import { cacheBytes } from "@/utils/cachePolicy";
import { storageAccessDenied } from "./storageQueryState";

export function StorageMaintenanceImpact({ location }: { location: StorageLocationOut }) {
  const revision = useSessionRevision();
  const { role } = usePermissions();
  if (role !== "super_admin" || (location.kind !== "filesystem" && !location.connection_id)) {
    return null;
  }
  return (
    <ImpactOwner
      key={`${revision}/${location.id}/${location.config_revision}`}
      location={location}
    />
  );
}

function ImpactOwner({ location }: { location: StorageLocationOut }) {
  const session = useAuthStore((state) => state.sessionId);
  const revision = useSessionRevision();
  const [open, setOpen] = useState(false);
  const query = useQuery({
    queryKey: [
      "storage-maintenance-impact",
      session,
      revision,
      location.id,
      location.config_revision,
    ],
    queryFn: ({ signal }) =>
      apiClient.get<Impact>(`/storage-locations/${location.id}/maintenance-impact`, { signal }),
    enabled: open,
    staleTime: 0,
    refetchOnWindowFocus: false,
  });
  const denied = storageAccessDenied(query.error);
  const changed =
    !!query.data &&
    (query.data.config_revision !== location.config_revision ||
      query.data.location_id !== location.id);
  const data = denied || changed ? undefined : query.data;
  const c = data?.counts;
  const rows = c
    ? [
        ["来源", `${c.sources} 个 · 启用 ${c.enabled_sources} · 已注销 ${c.unregistered_sources}`],
        ["业务关系", `${c.datasets} 个数据集 · ${c.projects} 个项目`],
        ["业务样本", `${c.items} 个 · 其中原地引用 ${c.reference_items}`],
        ["任务与标注", `${c.tasks} 个任务 · ${c.annotations} 条标注`],
        ["活动扫描与登记", `${c.active_scans} 个扫描 · ${c.active_registrations} 个预检/登记`],
        ["已启用自动同步", `${c.enabled_schedules} 个来源`],
        [
          "预览记录",
          `就绪 ${c.ready_previews} · 失败 ${c.failed_previews} · ${cacheBytes(c.preview_bytes)}`,
        ],
        [
          "完整原图缓存账本",
          `${c.cache_entries} 条 · ${cacheBytes(c.cache_bytes)} · 有效读租约 ${c.active_cache_read_leases}`,
        ],
        ["可关联临时输入", `活动状态 ${c.active_input_leases} · 待清理 ${c.cleanup_input_leases}`],
        [
          "有位置血缘的冻结版本",
          `就绪 ${c.ready_lineage_versions} · 未完成 ${c.unfinished_lineage_versions}`,
        ],
      ]
    : [];
  return (
    <details
      className="mt-4 rounded-lg border border-border text-sm"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary className="cursor-pointer px-4 py-3 font-medium">维护影响</summary>
      {open && (
        <div className="min-w-0 border-t border-border p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs leading-relaxed text-muted-foreground">
              统计已保存的关系，不读取源文件。位置修订 {location.config_revision}。
            </p>
            <Button size="sm" disabled={query.isFetching} onClick={() => void query.refetch()}>
              刷新维护盘点
            </Button>
          </div>
          {query.error && (
            <p role="alert" className="mb-3 text-xs leading-relaxed text-status-danger">
              {data ? "刷新失败，保留上次盘点。" : "无法读取维护盘点。"}
              {verifiedContentError(query.error)}
            </p>
          )}
          {changed && !denied && (
            <p role="alert" className="mb-3 text-xs text-status-warning">
              位置配置已变化，请关闭并重新打开位置详情后盘点。
            </p>
          )}
          {query.isPending && (
            <p role="status" className="text-xs text-muted-foreground">
              正在盘点已保存的关系…
            </p>
          )}
          {data && (
            <>
              <p className="mb-2 text-xs text-muted-foreground">
                统计于 {new Date(data.measured_at).toLocaleString("zh-CN")}
              </p>
              <dl className="divide-y divide-border">
                {rows.map(([label, value]) => (
                  <div key={label} className="grid gap-1 py-2 sm:grid-cols-2 sm:gap-4">
                    <dt className="text-xs text-muted-foreground">{label}</dt>
                    <dd className="min-w-0 break-words tabular-nums">{value}</dd>
                  </div>
                ))}
              </dl>
              <div className="mt-3 space-y-2 text-xs leading-relaxed text-muted-foreground">
                <p>
                  位置配置保存后，旧扫描和登记会在后续检查点停止写入，自动同步会在后续检查中阻塞。旧完整缓存将失配，后续回收仍受读租约保护；预览按当前策略和内容判断是否可复用。副本记录不代表文件此刻可读。
                </p>
                <p>
                  停用位置会拒绝新的引用原图读取，包括完整缓存。撤回目录使用权影响来源管理；已有任务仍按项目权限读取。已就绪冻结内容按项目权限独立保留。
                </p>
                <p>
                  临时输入按记录状态计数，含已到期但尚未转入清理的记录，不代表运行作业或有效下载凭证；仅包含当前任务仍可关联的输入，未覆盖全部预测、修复及孤儿输入。冻结数量仅包含已记录位置血缘的版本，托管和缺少位置血缘的历史未计入。零数量不能证明无引用；此盘点不提供归档、退役或回收许可。
                </p>
              </div>
            </>
          )}
        </div>
      )}
    </details>
  );
}
