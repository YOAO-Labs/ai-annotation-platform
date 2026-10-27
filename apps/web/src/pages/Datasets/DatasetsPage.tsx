import { useEffect, useId, useRef, useState } from "react";
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Database, FolderInput, Plus } from "lucide-react";
import { useDatasets } from "@/hooks/useDatasets";
import { useSessionGuard, useSessionRevision } from "@/hooks/useSessionGuard";
import { usePermissions } from "@/hooks/usePermissions";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { ImportDatasetWizard } from "@/components/datasets/ImportDatasetWizard";
import { CreateReferenceDatasetModal } from "@/components/datasets/CreateReferenceDatasetModal";
import { PageContainer } from "@/components/layout/PageContainer";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Card } from "@/components/ui/Card";
import { SearchInput } from "@/components/ui/SearchInput";
import { ListPagination } from "@/components/ui/ListPagination";
import { FilterGroup, FilterSelect } from "@/components/filters/FilterControls";
import { listStyles } from "@/components/ui/listStyles";
import {
  clampDatasetPage,
  datasetDetailPath,
  datasetsUrlCodec,
  legacyDatasetDetailPath,
  parseDatasetsUrl,
} from "./datasetsUrlState";

const typeLabels: Record<string, string> = {
  image: "图像",
  video: "视频",
  point_cloud: "3D 点云",
  multimodal: "多模态",
  other: "其他",
};
export function DatasetsPage() {
  const revision = useSessionRevision();
  const [params] = useSearchParams();
  const legacy = legacyDatasetDetailPath(params);
  if (legacy) return <Navigate replace to={legacy} />;
  if (["connections", "connectors", "数据连接器"].includes(params.get("tab") ?? ""))
    return <Navigate replace to="/storage?tab=locations" />;
  return <DatasetsList key={revision} />;
}
function DatasetsList() {
  const [params, setParams] = useSearchParams();
  const location = useLocation();
  const navigate = useNavigate();
  const state = parseDatasetsUrl(params);
  const stateHint = useId();
  const capture = useSessionGuard();
  const currentOwner = capture();
  const { role } = usePermissions();
  const canManage = role === "super_admin" || role === "project_admin";
  const client = useQueryClient();
  const [referenceOpen, setReferenceOpen] = useState(false);
  const createdReference = useRef<string | null>(null);
  const importOpen = ["1", "new"].includes(params.get("import") ?? "");
  const [query, setQuery] = useState(state.query);
  useEffect(() => setQuery(state.query), [state.query]);
  const search = useDebouncedValue(query, 250);
  const result = useDatasets({
    search: search.trim() || undefined,
    data_type: state.data_type,
    state: state.state,
    location_id: state.location_id,
    limit: state.pageSize,
    offset: state.page * state.pageSize,
  });
  const boundedPage =
    result.data && !result.isFetching
      ? clampDatasetPage(state.page, state.pageSize, result.data.total)
      : state.page;
  useEffect(() => {
    if (boundedPage === state.page || !currentOwner()) return;
    setParams(
      (current) =>
        datasetsUrlCodec.encode(current, { ...parseDatasetsUrl(current), page: boundedPage }),
      { replace: true },
    );
  }, [boundedPage, state.page, currentOwner, setParams]);
  const patch = (values: Partial<typeof state>) =>
    setParams((current) =>
      datasetsUrlCodec.encode(current, { ...parseDatasetsUrl(current), ...values }),
    );
  const setImportOpen = (open: boolean) =>
    setParams((current) => {
      const next = new URLSearchParams(current);
      if (open) next.set("import", "1");
      else next.delete("import");
      return next;
    });
  const rows = result.data?.items ?? [];
  const cell = "border-b border-border px-4 py-3 text-left text-sm";
  return (
    <PageContainer>
      <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">数据集</h1>
          <p className="mt-1 text-sm text-muted-foreground">浏览样本、管理来源与项目使用</p>
        </div>
        <div className="flex flex-wrap gap-2">
          {canManage && (
            <Button
              onClick={() => {
                createdReference.current = null;
                setReferenceOpen(true);
              }}
            >
              <FolderInput className="mr-1.5 size-3.5" />
              接入服务器目录
            </Button>
          )}
          {canManage && (
            <Button variant="primary" onClick={() => setImportOpen(true)}>
              <Plus className="mr-1.5 size-3.5" />
              新建数据集
            </Button>
          )}
        </div>
      </header>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>共 {result.data?.total ?? "—"} 个数据集</span>
        {canManage && (
          <Link className="text-primary hover:underline" to="/storage?tab=locations">
            管理存储位置
          </Link>
        )}
      </div>
      {state.location_id && (
        <div className="mb-3 flex items-center gap-2 rounded-md bg-muted p-3 text-xs">
          <span>当前仅显示所选存储位置的可见数据集</span>
          <Button size="xs" onClick={() => patch({ location_id: undefined, page: 0 })}>
            清除位置筛选
          </Button>
        </div>
      )}
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border p-3">
          <FilterGroup label="数据集筛选" hideLabel className="items-start gap-3">
            <label className="grid gap-1 text-sm">
              数据集状态
              <FilterSelect
                aria-label="数据集状态"
                aria-describedby={stateHint}
                value={state.state}
                onChange={(event) =>
                  patch({ state: event.target.value as typeof state.state, page: 0 })
                }
              >
                <option value="active">活动</option>
                <option value="archived">已归档</option>
                <option value="all">全部</option>
              </FilterSelect>
              <span id={stateHint} className="text-xs leading-relaxed text-muted-foreground">
                归档保留内容，可在详情中恢复。
              </span>
            </label>
            <label className="grid gap-1 text-sm">
              数据类型
              <FilterSelect
                aria-label="数据类型"
                value={state.data_type ?? ""}
                onChange={(e) =>
                  patch({
                    data_type: (e.target.value as typeof state.data_type) || undefined,
                    page: 0,
                  })
                }
              >
                <option value="">全部类型</option>
                {Object.entries(typeLabels)
                  .filter(([key]) => key !== "other")
                  .map(([key, label]) => (
                    <option key={key} value={key}>
                      {label}
                    </option>
                  ))}
              </FilterSelect>
            </label>
          </FilterGroup>
          <SearchInput
            placeholder="搜索数据集..."
            value={query}
            onChange={(value) => {
              setQuery(value);
              patch({ query: value, page: 0 });
            }}
            width={240}
          />
        </div>
        {result.error && (
          <div
            role="alert"
            className="flex flex-wrap items-center gap-2 p-4 text-xs text-status-danger"
          >
            {result.error.message}
            <Button size="sm" onClick={() => void result.refetch()}>
              重试数据集
            </Button>
          </div>
        )}
        <div className="overflow-x-auto">
          <table className={`${listStyles.table} min-w-full`}>
            <thead>
              <tr>
                {["数据集", "类型", "样本数", "关联项目", "最近变更", ""].map((label, i) => (
                  <th key={i} className={`${cell} bg-muted font-medium text-muted-foreground`}>
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {result.isLoading && (
                <tr>
                  <td colSpan={6} className="p-8 text-center text-sm text-muted-foreground">
                    正在加载数据集…
                  </td>
                </tr>
              )}
              {rows.map((ds) => (
                <tr key={ds.id}>
                  <td className={cell}>
                    <Link
                      className="inline-flex items-center gap-2 font-medium text-primary hover:underline"
                      to={datasetDetailPath(ds.id, location.search)}
                    >
                      <Database className="size-3.5 shrink-0" />
                      {ds.name}
                    </Link>
                    {ds.archived_at && (
                      <Badge variant="outline" className="ml-2">
                        已归档
                      </Badge>
                    )}
                    <p className="mt-1 max-w-80 truncate text-muted-foreground">
                      {ds.description || ds.display_id}
                    </p>
                  </td>
                  <td className={cell}>
                    <Badge variant="outline">{typeLabels[ds.data_type] ?? ds.data_type}</Badge>
                    {ds.has_scenes && <span className="ml-2 text-muted-foreground">含 Scene</span>}
                  </td>
                  <td className={`${cell} tabular-nums`}>{ds.file_count.toLocaleString()}</td>
                  <td className={`${cell} tabular-nums`}>{ds.project_count}</td>
                  <td className={`${cell} text-muted-foreground`}>
                    {new Date(ds.updated_at).toLocaleDateString("zh-CN")}
                  </td>
                  <td className={cell}>
                    <Link
                      className="text-primary hover:underline"
                      to={datasetDetailPath(ds.id, location.search)}
                    >
                      查看详情
                    </Link>
                  </td>
                </tr>
              ))}
              {!result.isLoading && !result.error && !rows.length && (
                <tr>
                  <td colSpan={6} className="p-8 text-center text-sm text-muted-foreground">
                    {state.query || state.data_type || state.location_id || state.state !== "active"
                      ? "没有匹配的数据集"
                      : "暂无数据集，接入服务器目录或上传文件开始"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <ListPagination
          label="数据集"
          page={state.page}
          pageSize={state.pageSize}
          total={result.data?.total ?? 0}
          loading={result.isFetching}
          onPageChange={(page) => patch({ page })}
          onPageSizeChange={(pageSize) => patch({ pageSize, page: 0 })}
        />
      </Card>
      {referenceOpen && (
        <CreateReferenceDatasetModal
          onCreated={(ds) => {
            if (!currentOwner()) return;
            createdReference.current = ds.id;
            void client.invalidateQueries({ queryKey: ["datasets"] });
          }}
          onClose={() => {
            if (!currentOwner()) return;
            setReferenceOpen(false);
            if (createdReference.current)
              navigate(
                datasetDetailPath(createdReference.current, location.search, { tab: "sources" }),
              );
          }}
        />
      )}
      <ImportDatasetWizard
        open={importOpen && canManage}
        onClose={() => setImportOpen(false)}
        onUploaded={() => {
          if (currentOwner()) void client.invalidateQueries({ queryKey: ["datasets"] });
        }}
      />
    </PageContainer>
  );
}
