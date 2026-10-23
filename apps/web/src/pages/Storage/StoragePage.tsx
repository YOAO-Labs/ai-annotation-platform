import { StorageUsagePanel } from "./StorageUsagePanel";
import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useEffect, useState } from "react";
import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { useRetryVideoAsset, useVideoAssetFailures } from "@/hooks/useStorage";
import { useToastStore } from "@/components/ui/Toast";
import { useQueryClient } from "@tanstack/react-query";
import type { VideoAssetFailureItem, VideoAssetKind } from "@/api/storage";
import { PageContainer } from "@/components/layout/PageContainer";
import { CopyableError } from "@/components/ui/CopyableError";
import { StorageLocationsConsole } from "@/components/connections/StorageLocationsConsole";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";

const VIDEO_ASSET_LABELS: Record<VideoAssetKind, string> = {
  probe: "Probe",
  poster: "Poster",
  frame_timetable: "Frame timetable",
  chunk: "Chunk",
  frame: "Frame cache",
};
const CARD_TITLE_CLASS = "m-0 text-sm font-semibold";

function assetDetail(asset: VideoAssetFailureItem): string {
  if (asset.asset_type === "chunk") return `chunk #${asset.chunk_id ?? "?"}`;
  if (asset.asset_type === "frame") {
    return `frame ${asset.frame_index ?? "?"} · ${asset.width ?? "?"}px ${asset.format ?? ""}`;
  }
  return asset.file_name;
}

function VideoAssetFailuresPanel() {
  const capture = useSessionGuard();
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const offset = page * pageSize;
  const { data, isLoading, isError } = useVideoAssetFailures(pageSize, offset);
  const retry = useRetryVideoAsset();
  const pushToast = useToastStore((s) => s.push);
  const items = data?.items ?? [];
  const total = data?.total ?? 0;

  useEffect(() => {
    if (isLoading) return;
    const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
    if (page > lastPage) setPage(lastPage);
  }, [isLoading, page, pageSize, total]);

  const onRetry = (asset: VideoAssetFailureItem) => {
    const current = capture();
    retry.mutate(
      {
        asset_type: asset.asset_type,
        dataset_item_id: asset.dataset_item_id,
        chunk_id: asset.chunk_id,
        frame_index: asset.frame_index,
        width: asset.width,
        format: asset.format === "jpeg" ? "jpeg" : asset.format === "webp" ? "webp" : null,
      },
      {
        onSuccess: () => {
          if (current()) pushToast({ msg: "已加入 media 重试队列", kind: "success" });
        },
        onError: (err) =>
          current() && pushToast({ msg: "重试失败", sub: (err as Error).message, kind: "error" }),
      },
    );
  };

  const thClass = "px-3 py-2.5 text-left text-xs font-medium text-muted-foreground";
  const tdClass = "px-3 py-2.5 align-middle";

  return (
    <div className="mt-4">
      <Card>
        <div className="flex justify-between gap-3 border-b border-border px-4 py-3.5">
          <div>
            <h3 className={CARD_TITLE_CLASS}>视频资产失败</h3>
            <p className="mt-[3px] text-xs text-muted-foreground">
              probe、poster、时间表和帧缓存失败会在这里集中处理。
            </p>
          </div>
          <span className="self-center">
            <Badge variant={total ? "danger" : "success"} dot>
              {total ? `${total} 个失败` : "正常"}
            </Badge>
          </span>
        </div>
        {isLoading ? (
          <div className="px-4 py-8.5 text-center text-sm text-muted-foreground">加载中...</div>
        ) : isError ? (
          <div className="px-4 py-8.5 text-center text-sm text-status-danger">
            无法加载视频资产状态
          </div>
        ) : items.length === 0 ? (
          <div className="px-4 py-8.5 text-center text-sm text-muted-foreground">
            <Icon name="check" size={26} className="mb-2 opacity-[0.28]" />
            <div>暂无视频资产失败</div>
          </div>
        ) : (
          <div>
            <div className="overflow-x-auto">
              <table className={listStyles.table}>
                <thead>
                  <tr className="border-b border-border">
                    <th className={thClass}>类型</th>
                    <th className={thClass}>视频</th>
                    <th className={thClass}>项目 / 任务</th>
                    <th className={thClass}>错误</th>
                    <th className={thClass}>更新时间</th>
                    <th className={thClass}>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((asset) => (
                    <tr key={asset.asset_key} className="border-b border-border">
                      <td className={tdClass}>
                        <Badge variant="outline">{VIDEO_ASSET_LABELS[asset.asset_type]}</Badge>
                      </td>
                      <td className={tdClass}>
                        <div className="font-medium">{asset.file_name}</div>
                        <div className="mt-0.5 text-xs text-muted-foreground">
                          {assetDetail(asset)}
                        </div>
                      </td>
                      <td className={tdClass}>
                        <div>{asset.project_name ?? "—"}</div>
                        <div className="mono mt-0.5 text-xs text-muted-foreground">
                          {asset.task_display_id ?? "—"}
                        </div>
                      </td>
                      <td className={`${tdClass} max-w-[420px]`}>
                        <CopyableError
                          message={asset.error}
                          label={`复制 ${asset.file_name} 的完整错误信息`}
                          className="w-full py-1 text-muted-foreground"
                          truncate
                        />
                      </td>
                      <td className={`${tdClass} text-xs text-muted-foreground`}>
                        {asset.updated_at ? new Date(asset.updated_at).toLocaleString() : "—"}
                      </td>
                      <td className={`${tdClass} whitespace-nowrap`}>
                        <Button
                          size="sm"
                          variant="primary"
                          disabled={retry.isPending}
                          onClick={() => onRetry(asset)}
                        >
                          <Icon name="refresh" size={11} />
                          重试
                        </Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
        <ListPagination
          label="视频资产失败"
          page={page}
          pageSize={pageSize}
          total={total}
          loading={isLoading}
          onPageChange={setPage}
          onPageSizeChange={(size) => {
            setPageSize(size);
            setPage(0);
          }}
        />
      </Card>
    </div>
  );
}

export function StoragePage() {
  const revision = useSessionRevision();
  return <StoragePageOwner key={revision} />;
}

function StoragePageOwner() {
  const qc = useQueryClient();
  const handleRefresh = () => {
    for (const key of [
      "storage-buckets",
      "storage-managed-original-reclamation",
      "managed-original-retention",
      "managed-original-verifications",
      "storage-source-locations",
      "storage-location",
      "storage-location-probes",
      "filesystem-locations",
      "storage-source-summary",
      "storage-bindings",
      "storage-connections",
      "storage-video-asset-failures",
    ]) {
      void qc.invalidateQueries({ queryKey: [key] });
    }
  };
  return (
    <PageContainer>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="mb-1 text-xl font-semibold">存储管理</h1>
          <p className="text-sm text-muted-foreground">
            维护存储位置与平台用途，查看节点能力和关联来源。
          </p>
        </div>
        <Button onClick={handleRefresh}>
          <Icon name="refresh" size={13} />
          刷新状态
        </Button>
      </div>
      <StorageLocationsConsole />
      <StorageUsagePanel />
      <VideoAssetFailuresPanel />
    </PageContainer>
  );
}
