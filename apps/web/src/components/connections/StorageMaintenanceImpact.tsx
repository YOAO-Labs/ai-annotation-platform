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
  const defaultStorage =
    location.kind === "s3" && location.id === "4fcc68cb-ce7a-5da2-ab39-143a6de6d0f7";
  if (
    role !== "super_admin" ||
    (location.kind !== "filesystem" && !location.connection_id && !defaultStorage)
  ) {
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
          {data && <StorageMaintenanceDetails data={data} />}
        </div>
      )}
    </details>
  );
}

export function StorageMaintenanceDetails({ data }: { data: Impact }) {
  const c = data.counts;
  const rows: [string, string][] = c
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
    <>
      <p className="mb-2 text-xs text-muted-foreground">
        统计于 {new Date(data.measured_at).toLocaleString("zh-CN")}
      </p>
      <section aria-label="保存目标" className="mb-4">
        <h4 className="text-sm font-medium">保存目标</h4>
        {typeof c?.target_records === "number" ? (
          <>
            <ImpactRows
              rows={[
                ["保存定位责任", `${c.target_records} 条`],
                [
                  "可证明业务关联",
                  `${c.target_related_datasets} 个数据集 · ${c.target_related_projects} 个项目 · ${c.target_related_tasks} 个任务`,
                ],
                ["保存在此的训练记录", `${c.target_versions} 个版本 · ${c.target_runs} 个实验`],
                ["当前用途绑定", `${c.current_bindings} 项`],
              ]}
            />
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
              责任按保存位置统计，包含在途、失败与退休记录。同一文件可能对应多份责任，数量不是物理对象数；当前用途绑定只决定后续新分配。
            </p>
            <details className="mt-3 rounded-lg border border-border">
              <summary className="cursor-pointer px-3 py-2 text-sm font-medium">
                查看九用途责任明细
              </summary>
              <div className="border-t border-border px-3 py-1">
                <ImpactRows
                  rows={[
                    ["标注文件", `${c.target_annotations} 条`],
                    ["数据集原件", `${c.target_datasets} 条`],
                    ["训练文件", `${c.target_training} 条`],
                    ["媒体缓存", `${c.target_media_cache} 条`],
                    ["审计归档", `${c.target_audit_archive} 条`],
                    ["导入暂存", `${c.target_import} 条`],
                    ["导出文件", `${c.target_export} 条`],
                    ["头像", `${c.target_avatars} 条`],
                    ["报告附件", `${c.target_bug_reports} 条`],
                  ]}
                />
              </div>
            </details>
            <p className="mt-3 text-xs leading-relaxed text-muted-foreground">
              全平台未定位历史责任：{c.unlocated_targets}{" "}
              条，未归入此位置。不会根据当前用途绑定猜测历史位置，也不要求先补齐历史才能维护此位置。
            </p>
          </>
        ) : (
          <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
            保存目标盘点暂不可用，请刷新维护盘点。
          </p>
        )}
      </section>
      <section aria-label="来源关系">
        <h4 className="text-sm font-medium">来源关系</h4>
        <ImpactRows rows={rows} />
      </section>
      <div className="mt-3 space-y-2 text-xs leading-relaxed text-muted-foreground">
        <p>
          位置配置保存后，旧扫描和登记会在后续检查点停止写入，自动同步会在后续检查中阻塞。旧完整缓存将失配，后续回收仍受读租约保护；预览按当前策略和内容判断是否可复用。副本记录不代表文件此刻可读。
        </p>
        <p>
          停用来源位置会拒绝新的引用原图读取，包括完整缓存。撤回目录使用权影响来源管理；已有任务仍按项目权限读取。已就绪冻结内容独立于源位置保留，其读取仍遵循输出保存位置的访问检查。
        </p>
        <p>
          来源关系中的临时输入按记录状态计数，含已到期但尚未转入清理的记录，不代表运行作业或有效下载凭证；仅包含当前任务仍可关联的输入，未覆盖全部预测、修复及孤儿输入。来源血缘的冻结数量仅包含已记录位置血缘的版本，托管和缺少位置血缘的历史未计入该项；保存目标另按自身地址统计。零数量不能证明无引用；此盘点不提供归档、退役或回收许可。
        </p>
      </div>
    </>
  );
}

function ImpactRows({ rows }: { rows: readonly (readonly [string, string])[] }) {
  return (
    <dl className="divide-y divide-border">
      {rows.map(([label, value]) => (
        <div key={label} className="grid gap-1 py-2 sm:grid-cols-2 sm:gap-4">
          <dt className="text-xs text-muted-foreground">{label}</dt>
          <dd className="min-w-0 break-words text-sm tabular-nums">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
