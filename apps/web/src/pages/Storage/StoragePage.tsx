import { useOffsetListPagination } from "@/hooks/useOffsetListPagination";
import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useEffect, useState } from "react";

import { Icon } from "@/components/ui/Icon";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { StatCard } from "@/components/ui/StatCard";
import { useRetryVideoAsset, useStorageBuckets, useVideoAssetFailures } from "@/hooks/useStorage";
import { useDatasets } from "@/hooks/useDatasets";
import { useToastStore } from "@/components/ui/Toast";
import { useQueryClient } from "@tanstack/react-query";
import type { DatasetResponse } from "@/api/datasets";
import type { BucketSummary, VideoAssetFailureItem, VideoAssetKind } from "@/api/storage";
import type { IconName } from "@/components/ui/Icon";
import { PageContainer } from "@/components/layout/PageContainer";
import { CopyableError } from "@/components/ui/CopyableError";
import { StorageLocationSourceSummary } from "@/components/connections/StorageLocationSourceSummary";
import { FilesystemLocationsPanel } from "@/components/connections/FilesystemLocationsPanel";

const TYPE_ICONS: Record<string, IconName> = {
  image: "image",
  video: "video",
  point_cloud: "cube",
  multimodal: "mm",
  other: "layers",
};

const TYPE_LABELS: Record<string, string> = {
  image: "图像",
  video: "视频",
  point_cloud: "3D 点云",
  multimodal: "多模态",
  other: "其他",
};

const ROLE_LABELS: Record<string, string> = {
  annotations: "标注文件",
  datasets: "数据集文件",
  "bug-reports": "Bug 截图",
  "media-cache": "派生媒体缓存",
  "audit-archive": "审计归档",
};

const VIDEO_ASSET_LABELS: Record<VideoAssetKind, string> = {
  probe: "Probe",
  poster: "Poster",
  frame_timetable: "Frame timetable",
  chunk: "Chunk",
  frame: "Frame cache",
};

const CARD_TITLE_CLASS = "m-0 text-sm font-semibold";

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const k = 1024;
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return `${(bytes / Math.pow(k, i)).toFixed(1)} ${units[i]}`;
}

function BucketCard({ bucket }: { bucket: BucketSummary }) {
  const isError = bucket.status === "error";
  return (
    <div className="mb-2.5 flex items-center gap-3 rounded-lg border border-border bg-card p-3.5">
      <div
        className={`flex size-[38px] shrink-0 items-center justify-center rounded-lg ${
          isError ? "bg-status-danger-soft" : "bg-status-positive-soft"
        }`}
      >
        <Icon
          name="db"
          size={18}
          className={isError ? "text-status-danger" : "text-status-positive"}
        />
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-semibold">{bucket.name}</div>
        <div className="mt-px text-xs text-muted-foreground">
          {ROLE_LABELS[bucket.role] ?? bucket.role}
          {isError && bucket.error && ` · ${bucket.error}`}
        </div>
      </div>
      <div className="shrink-0 text-right">
        <div className="text-sm font-semibold">{formatBytes(bucket.total_size_bytes)}</div>
        <div className="mt-px text-xs text-muted-foreground">
          {bucket.object_count.toLocaleString()} 个对象
        </div>
      </div>
      <span className="shrink-0">
        <Badge variant={isError ? "danger" : "success"} dot>
          {isError ? "连接失败" : "已连接"}
        </Badge>
      </span>
    </div>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-muted px-3 py-2.5">
      <div className="mb-0.5 text-xs text-muted-foreground">{label}</div>
      <div className="text-sm font-medium">{value}</div>
    </div>
  );
}

function DatasetStorageRow({ ds }: { ds: DatasetResponse & { total_size?: number } }) {
  const cellClass = "border-b border-border px-3 py-2.5 align-middle";
  return (
    <tr>
      <td className={`${cellClass} pl-4`}>
        <div className="flex items-center gap-2">
          <Icon
            name={TYPE_ICONS[ds.data_type] || "layers"}
            size={14}
            className="text-muted-foreground"
          />
          <div>
            <div className="text-sm font-medium">{ds.name}</div>
            <div className="text-xs text-muted-foreground">{ds.display_id}</div>
          </div>
        </div>
      </td>
      <td className={cellClass}>
        <Badge variant="outline">{TYPE_LABELS[ds.data_type] || ds.data_type}</Badge>
      </td>
      <td className={cellClass}>
        <span className="mono text-sm">{ds.file_count.toLocaleString()}</span>
      </td>
      <td className={cellClass}>
        <span className="mono text-sm">
          {ds.total_size !== undefined ? formatBytes(ds.total_size) : "—"}
        </span>
      </td>
      <td className={cellClass}>
        <span className="mono text-sm">{ds.project_count}</span>
      </td>
    </tr>
  );
}

function assetDetail(asset: VideoAssetFailureItem): string {
  if (asset.asset_type === "chunk") return `chunk #${asset.chunk_id ?? "?"}`;
  if (asset.asset_type === "frame") {
    return `frame ${asset.frame_index ?? "?"} · ${asset.width ?? "?"}px ${asset.format ?? ""}`;
  }
  return asset.file_name;
}

function VideoAssetFailuresPanel() {
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
        onSuccess: () => pushToast({ msg: "已加入 media 重试队列", kind: "success" }),
        onError: (err) =>
          pushToast({ msg: "重试失败", sub: (err as Error).message, kind: "error" }),
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
  const qc = useQueryClient();
  const { data: bucketsData, isError: bucketsError } = useStorageBuckets();
  const pagination = useOffsetListPagination();
  const {
    data: datasetsData,
    isLoading: datasetsLoading,
    isFetching: datasetsFetching,
  } = useDatasets({ limit: pagination.pageSize, offset: pagination.offset });

  const buckets = bucketsData?.items ?? [];
  const datasets = datasetsData?.items ?? [];
  const totalDatasets = datasetsData?.total ?? 0;
  useEffect(() => {
    if (!datasetsData || datasetsFetching) return;
    const lastPage = Math.max(0, Math.ceil(totalDatasets / pagination.pageSize) - 1);
    if (pagination.page > lastPage) pagination.setPage(lastPage);
  }, [datasetsData, datasetsFetching, totalDatasets, pagination]);

  const handleRefresh = () => {
    qc.invalidateQueries({ queryKey: ["storage-source-locations"] });
    qc.invalidateQueries({ queryKey: ["storage-source-summary"] });
    qc.invalidateQueries({ queryKey: ["storage-buckets"] });
    qc.invalidateQueries({ queryKey: ["datasets"] });
    qc.invalidateQueries({ queryKey: ["storage-video-asset-failures"] });
  };

  const datasetThClass =
    "border-b border-border bg-muted px-3 py-2 text-left text-xs font-medium text-muted-foreground";

  return (
    <PageContainer>
      {/* Header */}
      <div className="mb-5 flex items-end justify-between gap-6 max-[760px]:flex-col max-[760px]:items-start">
        <div>
          <h1 className="mb-1 text-xl font-semibold">存储管理</h1>
          <p className="text-sm text-muted-foreground">查看存储后端状态与数据集分布</p>
        </div>
        <Button onClick={handleRefresh}>
          <Icon name="refresh" size={13} /> 刷新状态
        </Button>
      </div>

      <FilesystemLocationsPanel />
      <StorageLocationSourceSummary />
      {/* Stats */}
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard icon="db" label="存储后端" value="MinIO (S3)" />
        <StatCard icon="folder" label="存储桶" value={String(buckets.length)} />
        <StatCard icon="layers" label="数据集数量" value={String(totalDatasets)} />
        <StatCard
          icon="activity"
          label="总容量"
          value={bucketsData ? formatBytes(bucketsData.total_size_bytes) : "—"}
        />
      </div>

      <div className="grid grid-cols-[repeat(auto-fit,minmax(320px,1fr))] gap-4">
        {/* Storage backends */}
        <Card>
          <div className="border-b border-border px-4 py-3.5">
            <h3 className={CARD_TITLE_CLASS}>存储桶</h3>
          </div>
          <div className="p-4">
            {bucketsError && buckets.length === 0 ? (
              <div className="py-5 text-center text-sm text-status-danger">
                <Icon name="db" size={24} className="mb-1.5 opacity-40" />
                <div>无法连接存储后端</div>
              </div>
            ) : (
              buckets.map((b) => <BucketCard key={b.name} bucket={b} />)
            )}
            <div className="mt-1 grid grid-cols-[repeat(auto-fit,minmax(180px,1fr))] gap-2.5">
              <InfoItem label="存储类型" value="S3 兼容 (MinIO)" />
              <InfoItem label="协议" value="HTTP (开发环境)" />
              <InfoItem
                label="总对象数"
                value={bucketsData ? bucketsData.total_object_count.toLocaleString() : "—"}
              />
              <InfoItem
                label="总占用空间"
                value={bucketsData ? formatBytes(bucketsData.total_size_bytes) : "—"}
              />
            </div>
          </div>
        </Card>

        {/* Dataset storage overview */}
        <Card>
          <div className="border-b border-border px-4 py-3.5">
            <h3 className={CARD_TITLE_CLASS}>数据集存储概览</h3>
          </div>
          {datasets.length > 0 ? (
            <table className={listStyles.table}>
              <thead>
                <tr>
                  {["数据集", "类型", "文件数", "容量", "关联项目"].map((h, i) => (
                    <th key={i} className={i === 0 ? `${datasetThClass} pl-4` : datasetThClass}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {datasets.map((ds) => (
                  <DatasetStorageRow
                    key={ds.id}
                    ds={ds as DatasetResponse & { total_size?: number }}
                  />
                ))}
              </tbody>
            </table>
          ) : (
            <div className="px-4 py-8 text-center text-sm text-muted-foreground">
              <Icon name="layers" size={28} className="mb-2 opacity-[0.28]" />
              <div>暂无数据集</div>
            </div>
          )}
          <ListPagination
            label="存储数据集"
            page={pagination.page}
            pageSize={pagination.pageSize}
            total={totalDatasets}
            loading={datasetsLoading}
            onPageChange={pagination.setPage}
            onPageSizeChange={pagination.setPageSize}
          />
        </Card>
      </div>

      <VideoAssetFailuresPanel />
    </PageContainer>
  );
}
