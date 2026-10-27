import { useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useProtocolCapabilities, type ProtocolTask } from "@/api/mlCapabilities";
import { useModelCatalog } from "@/api/modelCatalog";
import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useListPagination } from "@/hooks/useListPagination";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/shadcn/ui/sheet";

const geometryLabels: Record<string, string> = {
  bbox: "矩形框",
  rotated_bbox: "旋转框",
  polygon: "多边形",
  multi_polygon: "多区域多边形",
  keypoint: "关键点",
  mask: "像素掩码",
  none: "类别标签（无几何）",
};

export function ProtocolCatalogPanel() {
  const protocol = useProtocolCapabilities();
  const models = useModelCatalog({ page_size: 1 });
  const [search, setSearch] = useSearchParams();
  const [detail, setDetail] = useState<ProtocolTask | null>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const q = search.get("protocol_q") ?? "";
  const task = search.get("protocol_task") ?? "";
  const tasks =
    protocol.data?.tasks.filter(
      (item) =>
        (!task || item.id === task) &&
        `${item.label} ${item.id} ${item.summary} ${item.typical_models.join(" ")}`
          .toLowerCase()
          .includes(q.trim().toLowerCase()),
    ) ?? [];
  const pagination = useListPagination(tasks, `${q}|${task}`);
  const modelUrl = (id: string) => `/model-market?model_task=${encodeURIComponent(id)}`;
  function update(key: string, value: string) {
    setSearch(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(key, value);
        else next.delete(key);
        return next;
      },
      { replace: true },
    );
  }
  function inputs(item: ProtocolTask) {
    return item.default_modalities.map(
      (id) => protocol.data?.modalities.find((m) => m.id === id)?.label ?? id,
    );
  }
  function outputs(item: ProtocolTask) {
    return item.default_geometry.map(
      (id) => geometryLabels[id] ?? protocol.data?.geometries.find((g) => g.id === id)?.label ?? id,
    );
  }
  return (
    <section aria-label="协议能力目录" className="space-y-3">
      <p className="text-sm text-muted-foreground">
        先按任务了解输入与输出，再到模型目录选择具体型号。协议支持范围不代表已有可运行的模型。
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="block min-w-48 max-w-md flex-1 text-xs text-muted-foreground">
          搜索协议能力
          <input
            value={q}
            onChange={(event) => update("protocol_q", event.target.value)}
            placeholder="任务名称、协议标识或参考模型"
            className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground"
          />
        </label>
        <label className="text-xs text-muted-foreground">
          任务
          <select
            value={task}
            onChange={(event) => update("protocol_task", event.target.value)}
            className="mt-1 block h-9 rounded-md border border-input bg-background px-2 text-xs text-foreground"
          >
            <option value="">全部任务</option>
            {protocol.data?.tasks.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {protocol.isPending && (
        <p role="status" className="py-8 text-center text-sm text-muted-foreground">
          正在读取协议能力…
        </p>
      )}
      {protocol.isError && (
        <div role="alert" className="rounded-md border border-border p-4">
          <p className="mb-2 text-sm">协议能力读取失败</p>
          <Button size="sm" onClick={() => void protocol.refetch()}>
            重试
          </Button>
        </div>
      )}
      {protocol.isSuccess && (
        <>
          <p className="text-xs text-muted-foreground">
            {tasks.length} 类任务 · 目录数量按具体规格 / 版本统计，可用状态以模型目录为准
          </p>
          <div className={listStyles.surface}>
            <div
              aria-hidden="true"
              className="hidden gap-4 bg-muted px-4 py-3 text-xs text-muted-foreground md:grid md:grid-cols-12"
            >
              <span className="col-span-5">任务与用途</span>
              <span className="col-span-2">适用输入</span>
              <span className="col-span-3">标注输出</span>
              <span className="col-span-2">目录与协议</span>
            </div>
            {pagination.items.map((item) => (
              <article
                key={item.id}
                className={`${listStyles.row} grid gap-3 md:grid-cols-12 md:gap-4`}
              >
                <div className="min-w-0 md:col-span-5">
                  <h2 className="text-sm font-semibold">
                    {item.label}
                    <span className="ml-2 font-mono text-xs font-normal text-muted-foreground">
                      {item.id}
                    </span>
                  </h2>
                  <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground">
                    {item.summary}
                  </p>
                </div>
                <div className="md:col-span-2">
                  <span className="mb-1.5 block text-xs text-muted-foreground md:hidden">
                    适用输入
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {inputs(item).map((label) => (
                      <Badge key={label}>{label}</Badge>
                    ))}
                  </div>
                </div>
                <div className="md:col-span-3">
                  <span className="mb-1.5 block text-xs text-muted-foreground md:hidden">
                    标注输出
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {outputs(item).map((label) => (
                      <Badge key={label} variant="outline">
                        {label}
                      </Badge>
                    ))}
                  </div>
                  {!item.default_geometry.length && (
                    <p className="text-xs text-muted-foreground">由具体模型声明</p>
                  )}
                </div>
                <div className="flex flex-wrap items-start gap-x-4 gap-y-2 text-xs md:col-span-2 md:flex-col">
                  <Link className="text-primary hover:underline" to={modelUrl(item.id)}>
                    查看模型{models.data ? `（${models.data.task_counts?.[item.id] ?? 0}）` : ""}
                  </Link>
                  <button
                    type="button"
                    className="text-muted-foreground hover:text-foreground hover:underline"
                    onClick={(event) => {
                      opener.current = event.currentTarget;
                      setDetail(item);
                    }}
                  >
                    查看协议
                  </button>
                  {!models.data && (
                    <span className="text-muted-foreground">
                      {models.isError ? "目录数量暂不可用" : "数量读取中…"}
                    </span>
                  )}
                </div>
              </article>
            ))}
            {!tasks.length && (
              <p className="px-4 py-10 text-center text-sm text-muted-foreground">
                没有匹配的协议能力。
              </p>
            )}
          </div>
          <ListPagination
            label="协议能力"
            {...pagination}
            onPageChange={pagination.setPage}
            onPageSizeChange={pagination.setPageSize}
          />
        </>
      )}
      <Sheet
        open={!!detail}
        onOpenChange={(open) => {
          if (!open) setDetail(null);
        }}
      >
        {detail && (
          <SheetContent
            className="w-full gap-0 sm:max-w-xl"
            closeButtonLabel="关闭协议详情"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              opener.current?.focus();
            }}
          >
            <SheetHeader className="shrink-0 border-b border-border pr-10">
              <SheetTitle>{detail.label} · 协议详情</SheetTitle>
              <SheetDescription>{detail.summary}</SheetDescription>
            </SheetHeader>
            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 text-xs">
              <dl className="grid grid-cols-2 gap-4">
                <div>
                  <dt className="mb-2 text-muted-foreground">适用输入</dt>
                  <dd>{inputs(detail).join("、") || "由具体模型声明"}</dd>
                </div>
                <div>
                  <dt className="mb-2 text-muted-foreground">标注输出</dt>
                  <dd>{outputs(detail).join("、") || "由具体模型声明"}</dd>
                </div>
              </dl>
              <section>
                <h3 className="mb-2 font-medium">参考模型</h3>
                <div className="flex flex-wrap gap-1.5">
                  {detail.typical_models.map((name) => (
                    <Badge key={name} variant="outline">
                      {name}
                    </Badge>
                  ))}
                </div>
                <p className="mt-2 text-muted-foreground">
                  用于理解任务类型，不代表已在当前平台部署。
                </p>
              </section>
              <section>
                <h3 className="mb-2 font-medium">接口与字段约定</h3>
                <p className="break-words rounded-md border border-border bg-muted/30 p-3 font-mono leading-relaxed text-muted-foreground">
                  {detail.protocol_notes}
                </p>
              </section>
              {!!detail.suggested_backends.length && (
                <section>
                  <h3 className="mb-2 font-medium">参考后端实现</h3>
                  <ul className="divide-y divide-border rounded-md border border-border">
                    {detail.suggested_backends.map((backend) => (
                      <li key={backend.repo_url} className="space-y-1.5 p-3">
                        <a
                          href={backend.repo_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-primary hover:underline"
                        >
                          {backend.name} ↗
                        </a>
                        <p className="leading-relaxed text-muted-foreground">{backend.summary}</p>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
            <div className="shrink-0 border-t border-border p-4 text-xs">
              <Link className="text-primary hover:underline" to={modelUrl(detail.id)}>
                到模型目录选择型号 →
              </Link>
            </div>
          </SheetContent>
        )}
      </Sheet>
    </section>
  );
}
