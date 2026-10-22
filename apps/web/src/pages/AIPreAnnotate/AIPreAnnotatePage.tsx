import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { ProjectCardGrid } from "./components/ProjectCardGrid";
import { ProjectDetailPanel } from "./components/ProjectDetailPanel";
import { useAIPreProjectSummary } from "@/hooks/useBulkPreannotateActions";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Input } from "@/components/shadcn/ui/input";

export default function AIPreAnnotatePage() {
  const summaryQ = useAIPreProjectSummary();
  const items = useMemo(() => summaryQ.data?.items ?? [], [summaryQ.data?.items]);

  const [search, setSearch] = useState("");
  const [params, setParams] = useSearchParams();
  const selectedProjectId = params.get("project");
  const setSelectedProjectId = (id: string | null) => {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      if (id) next.set("project", id);
      else {
        next.delete("project");
        next.delete("catalog_model");
      }
      return next;
    });
  };

  const selectedSummary = useMemo(
    () => items.find((it) => it.project_id === selectedProjectId),
    [items, selectedProjectId],
  );

  const filteredItems = useMemo(() => {
    const query = search.trim().toLocaleLowerCase();
    return query
      ? items.filter((item) =>
          [item.project_name, item.project_display_id, item.ml_backend_name].some((value) =>
            value?.toLocaleLowerCase().includes(query),
          ),
        )
      : items;
  }, [items, search]);

  const pagination = useListPagination(filteredItems, search);

  if (selectedProjectId)
    return (
      <ProjectDetailPanel
        projectId={selectedProjectId}
        summary={selectedSummary}
        initialCatalogModelId={params.get("catalog_model")}
        onBack={() => setSelectedProjectId(null)}
      />
    );

  return (
    <section className="min-w-0 space-y-4" aria-labelledby="ai-pre-projects-title">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 id="ai-pre-projects-title" className="text-sm font-semibold">
            选择项目
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            仅展示已接入推理服务的项目，选择后配置批次与预标流程。
          </p>
        </div>
        <div className="flex w-full min-w-0 items-center gap-2 sm:w-auto">
          <Input
            type="search"
            aria-label="搜索预标项目"
            placeholder="搜索项目、编号或推理服务"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            className="h-8 sm:w-64"
          />
          <Button
            size="sm"
            variant="ghost"
            aria-label="刷新项目列表"
            title="刷新项目列表"
            disabled={summaryQ.isFetching}
            onClick={() => void summaryQ.refetch()}
          >
            <Icon
              name="refresh"
              size={14}
              className={
                summaryQ.isFetching ? "animate-spin motion-reduce:animate-none" : undefined
              }
            />
          </Button>
        </div>
      </div>
      {summaryQ.isError && (
        <Card>
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 p-4">
            <div>
              <p className="text-sm font-medium text-status-danger">项目列表加载失败</p>
              <p className="mt-1 text-xs text-muted-foreground">
                请检查网络连接，或稍后重新加载项目列表。
              </p>
            </div>
            <Button
              size="sm"
              disabled={summaryQ.isFetching}
              onClick={() => void summaryQ.refetch()}
            >
              重新加载
            </Button>
          </div>
        </Card>
      )}
      {!summaryQ.isLoading && !summaryQ.isError && items.length > 0 && (
        <p className="text-xs tabular-nums text-muted-foreground" role="status">
          {search.trim()
            ? `匹配 ${filteredItems.length} / ${items.length} 个项目`
            : `共 ${items.length} 个项目`}
        </p>
      )}
      {items.length > 0 && filteredItems.length === 0 ? (
        <Card>
          <div className="space-y-2 p-8 text-center">
            <p className="text-sm font-medium">没有匹配的项目</p>
            <p className="text-xs text-muted-foreground">
              换一个项目名称、编号或推理服务名称试试。
            </p>
            <Button size="sm" onClick={() => setSearch("")}>
              清除搜索
            </Button>
          </div>
        </Card>
      ) : (
        (!summaryQ.isError || items.length > 0) && (
          <ProjectCardGrid
            items={pagination.items}
            isLoading={summaryQ.isLoading}
            onSelect={setSelectedProjectId}
          />
        )
      )}
      <ListPagination
        label="预标项目"
        {...pagination}
        loading={summaryQ.isLoading}
        onPageChange={pagination.setPage}
        onPageSizeChange={pagination.setPageSize}
      />
    </section>
  );
}
