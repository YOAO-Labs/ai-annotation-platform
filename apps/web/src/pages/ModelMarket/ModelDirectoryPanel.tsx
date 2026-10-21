import { useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  classSourceLabel,
  isImageDetectionEntry,
  useModelCatalog,
  useModelDirectory,
  type CatalogEntry,
} from "@/api/modelCatalog";
import { useProtocolCapabilities } from "@/api/mlCapabilities";
import { ListPagination, listPageSize } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription,
} from "@/components/shadcn/ui/sheet";
import {
  groupModelDirectory,
  variantLabel,
  type ModelDirectoryGroup,
} from "./modelDirectoryGroups";

const selectClass = "h-9 rounded-md border border-input bg-background px-2 text-xs text-foreground";
const publicationLabels: Record<string, string> = {
  builtin: "内置",
  candidate: "候选",
  current: "当前发布",
  previous: "曾发布",
  archived: "已停用",
};

export function ModelDirectoryPanel() {
  const [search, setSearch] = useSearchParams();
  const [selection, setSelection] = useState<Record<string, string>>({});
  const [detailGroupId, setDetailGroupId] = useState<string | null>(null);
  const opener = useRef<HTMLButtonElement | null>(null);
  const projectId = search.get("model_project") ?? "";
  const task = search.get("model_task") ?? "";
  const source = search.get("model_source") ?? "";
  const status = search.get("model_status") ?? "";
  const q = search.get("model_q") ?? "";
  const catalog = useModelDirectory({ project_id: projectId, task, source, status, q });
  const projectOptions = useModelCatalog({ page_size: 1 });
  const protocol = useProtocolCapabilities();
  const pageSize = listPageSize(search.get("model_page_size"));
  const groups = groupModelDirectory(catalog.data?.items ?? []);
  const pageCount = Math.max(1, Math.ceil(groups.length / pageSize));
  const requestedPage = Number(search.get("model_page")) || 1;
  const page = Math.min(pageCount, Math.max(1, Math.trunc(requestedPage)));
  const detailGroup = groups.find((group) => group.id === detailGroupId);
  const selected = (group: ModelDirectoryGroup) =>
    group.entries.find((entry) => entry.id === selection[group.id]) ?? group.entries[0];
  const detail = detailGroup ? selected(detailGroup) : undefined;
  const taskLabel = (id: string) =>
    protocol.data?.tasks.find((item) => item.id === id)?.label ?? id;

  function update(key: string, value: string) {
    setDetailGroupId(null);
    setSearch(
      (current) => {
        const next = new URLSearchParams(current);
        if (value) next.set(key, value);
        else next.delete(key);
        if (key !== "model_page") next.delete("model_page");
        return next;
      },
      { replace: true },
    );
  }
  function showDetail(group: ModelDirectoryGroup, button: HTMLButtonElement) {
    opener.current = button;
    setDetailGroupId(group.id);
  }
  function selectVariant(group: ModelDirectoryGroup, id: string) {
    setSelection((current) => ({ ...current, [group.id]: id }));
  }

  return (
    <section aria-label="模型目录" className="space-y-3">
      <p className="text-sm text-muted-foreground">
        同一系列的型号合并展示。在详情中选择具体规格，查看类别、可用状态并进入项目使用。
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <label className="min-w-48 flex-1 text-xs text-muted-foreground">
          搜索模型
          <input
            className="mt-1 h-9 w-full rounded-md border border-input bg-background px-3 text-sm text-foreground"
            value={q}
            onChange={(event) => update("model_q", event.target.value)}
            placeholder="模型名称、系列或规格"
          />
        </label>
        <label className="text-xs text-muted-foreground">
          项目
          <select
            className={`${selectClass} mt-1 block max-w-64`}
            value={projectId}
            onChange={(event) => update("model_project", event.target.value)}
          >
            <option value="">全部可管理项目</option>
            {projectOptions.data?.projects?.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          任务
          <select
            className={`${selectClass} mt-1 block`}
            value={task}
            onChange={(event) => update("model_task", event.target.value)}
          >
            <option value="">全部任务</option>
            {protocol.data?.tasks.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          来源
          <select
            className={`${selectClass} mt-1 block`}
            value={source}
            onChange={(event) => update("model_source", event.target.value)}
          >
            <option value="">全部来源</option>
            <option value="builtin">内置模型</option>
            <option value="project_trained">项目训练</option>
          </select>
        </label>
        <label className="text-xs text-muted-foreground">
          发布状态
          <select
            className={`${selectClass} mt-1 block`}
            value={status}
            onChange={(event) => update("model_status", event.target.value)}
          >
            <option value="">全部状态</option>
            {Object.entries(publicationLabels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      {catalog.isPending && (
        <p role="status" className="py-8 text-center text-sm text-muted-foreground">
          正在读取模型目录…
        </p>
      )}
      {catalog.isError && (
        <div role="alert" className="rounded-md border border-border p-4">
          <p className="mb-2 text-sm">模型目录读取失败，请重试。</p>
          <Button size="sm" onClick={() => void catalog.refetch()}>
            重试
          </Button>
        </div>
      )}
      {catalog.data && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              <strong className="font-medium text-foreground">{groups.length} 组模型</strong> ·{" "}
              {catalog.data.items.length} 个规格 / 版本
            </span>
            <span>各规格信息不同时，请在详情中查看</span>
          </div>
          <div className={listStyles.surface}>
            <div className={listStyles.scroll}>
              <table className={listStyles.table}>
                <thead>
                  <tr>
                    {["模型 / 来源", "型号规格", "任务", "类别", "使用状态", "操作"].map(
                      (label) => (
                        <th key={label} scope="col">
                          {label}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody>
                  {groups.slice((page - 1) * pageSize, page * pageSize).map((group) => {
                    const entry = group.entries[0];
                    return (
                      <tr key={group.id} className="align-top">
                        <td className="min-w-44 px-3 py-3">
                          <button
                            type="button"
                            onClick={(event) => showDetail(group, event.currentTarget)}
                            className="text-left font-medium text-foreground hover:underline"
                          >
                            {group.name}
                            {group.series && (
                              <span className="mt-1 block font-mono text-primary">
                                {group.series}
                              </span>
                            )}
                          </button>
                          <p className="mt-1 text-muted-foreground">
                            {entry.project?.name ?? "内置模型"} · {entry.framework ?? "未声明框架"}
                          </p>
                        </td>
                        <td className="min-w-40 px-3 py-3">
                          <VariantLabels group={group} />
                        </td>
                        <td className="whitespace-nowrap px-3 py-3">{taskLabel(entry.task)}</td>
                        <td className="min-w-32 px-3 py-3">
                          {group.entries.every(
                            (item) =>
                              classCountLabel(item) === classCountLabel(entry) &&
                              classSourceLabel(item) === classSourceLabel(entry),
                          ) ? (
                            <ClassSummary entry={entry} />
                          ) : (
                            <p className="text-muted-foreground">类别因规格而异</p>
                          )}
                        </td>
                        <td className="min-w-40 px-3 py-3">
                          {group.entries.every(
                            (item) =>
                              item.operations.preannotate.allowed ===
                                entry.operations.preannotate.allowed &&
                              usageLabel(item) === usageLabel(entry) &&
                              deploymentLabel(item) === deploymentLabel(entry),
                          ) ? (
                            <UsageSummary entry={entry} />
                          ) : (
                            <p className="text-muted-foreground">使用状态因规格而异</p>
                          )}
                        </td>
                        <td className="min-w-28 px-3 py-3">
                          <div className="flex flex-col items-start gap-2 whitespace-nowrap">
                            <button
                              type="button"
                              onClick={(event) => showDetail(group, event.currentTarget)}
                              className="font-medium text-primary hover:underline"
                            >
                              查看详情
                            </button>
                            {group.entries.length === 1 && (
                              <ModelActions entry={entry} projectId={projectId} />
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            {!groups.length && (
              <p className="px-4 py-10 text-center text-sm text-muted-foreground">
                没有匹配的模型。可调整筛选，或在项目的模型管理中注册训练结果。
              </p>
            )}
          </div>
          <ListPagination
            label="模型目录"
            page={page - 1}
            pageSize={pageSize}
            total={groups.length}
            onPageChange={(next) => update("model_page", String(next + 1))}
            onPageSizeChange={(size) => update("model_page_size", String(size))}
          />
        </>
      )}
      <Sheet
        open={!!detail}
        onOpenChange={(open) => {
          if (!open) setDetailGroupId(null);
        }}
      >
        {detail && detailGroup && (
          <SheetContent
            className="w-full gap-0 sm:max-w-xl"
            closeButtonLabel="关闭模型详情"
            onCloseAutoFocus={(event) => {
              event.preventDefault();
              opener.current?.focus();
            }}
          >
            <SheetHeader className="shrink-0 border-b border-border pr-10">
              <SheetTitle className="text-base">{detail.name}</SheetTitle>
              <SheetDescription>
                {[detailGroup.series, taskLabel(detail.task), detail.project?.name ?? "内置模型"]
                  .filter(Boolean)
                  .join(" · ")}
              </SheetDescription>
            </SheetHeader>
            <div className="min-h-0 flex-1 space-y-5 overflow-y-auto p-4 text-xs">
              <section aria-label="型号规格">
                <h3 className="mb-2 font-medium">
                  型号规格{detailGroup.entries.length > 1 && `（${detailGroup.entries.length}）`}
                </h3>
                <VariantOptions
                  group={detailGroup}
                  entry={detail}
                  onSelect={(id) => selectVariant(detailGroup, id)}
                />
                {!!Object.keys(detail.model_variants ?? {}).length && (
                  <p className="mt-2 break-words font-mono text-muted-foreground">
                    {Object.entries(detail.model_variants ?? {})
                      .map(([key, value]) => `${key}=${value}`)
                      .join(" · ")}
                  </p>
                )}
              </section>
              <section
                aria-label="使用条件"
                className="rounded-md border border-border bg-muted/30 p-3"
              >
                <UsageSummary entry={detail} />
              </section>
              <section aria-label="原生类别">
                <h3 className="mb-2 font-medium">
                  原生类别
                  {detail.capabilities.classes?.length
                    ? `（${detail.capabilities.classes.length}）`
                    : ""}
                </h3>
                <p className="mb-3 text-muted-foreground">{classSourceLabel(detail)}</p>
                <div className="flex flex-wrap gap-1.5">
                  {detail.capabilities.classes?.map((item) => (
                    <Badge key={String(item.index)} variant="outline">
                      {String(item.index)} · {String(item.name)}
                    </Badge>
                  ))}
                  {!detail.capabilities.classes?.length && (
                    <p className="text-muted-foreground">
                      {detail.class_metadata.class_source === "request_prompt"
                        ? "类别由每次请求的提示确定。"
                        : "当前没有可展示的类别元数据。"}
                    </p>
                  )}
                </div>
              </section>
              <section aria-label="部署实例">
                <h3 className="mb-2 font-medium">部署实例</h3>
                <ul className="divide-y divide-border rounded-md border border-border">
                  {(detail.placements ?? []).map((placement) => (
                    <li key={placement.registry_id} className="space-y-1 p-3">
                      <p className="font-medium">{placement.name}</p>
                      <p
                        className={
                          placement.ready ? "text-status-positive" : "text-muted-foreground"
                        }
                      >
                        {placement.ready ? "部署就绪" : (placement.reason ?? "尚未就绪")}
                      </p>
                    </li>
                  ))}
                </ul>
                {!detail.placements?.length && <p className="text-muted-foreground">尚未部署</p>}
              </section>
              <dl className="space-y-2 border-t border-border pt-4">
                <dt className="font-medium">权重摘要</dt>
                <dd className="break-all font-mono text-muted-foreground">
                  {detail.class_metadata.artifact_sha256 ?? "尚无已核验权重身份"}
                </dd>
              </dl>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-3 border-t border-border p-4 text-xs">
              <ModelActions entry={detail} projectId={projectId} />
              {!detail.project && !projectId && (
                <p className="text-muted-foreground">先在目录选择项目，再查看可用的预标注入口。</p>
              )}
            </div>
          </SheetContent>
        )}
      </Sheet>
    </section>
  );
}

function VariantLabels({ group }: { group: ModelDirectoryGroup }) {
  return (
    <ul aria-label={`${group.name} ${group.series} 型号规格`} className="flex flex-wrap gap-1.5">
      {group.entries.map((variant) => (
        <li
          key={variant.id}
          className="rounded bg-muted px-2 py-1 font-mono text-xs text-muted-foreground"
        >
          {variantLabel(variant)}
        </li>
      ))}
    </ul>
  );
}

function VariantOptions({
  group,
  entry,
  onSelect,
}: {
  group: ModelDirectoryGroup;
  entry: CatalogEntry;
  onSelect: (id: string) => void;
}) {
  if (group.entries.length === 1) return <VariantLabels group={group} />;

  return (
    <div
      role="group"
      aria-label={`${group.name} ${group.series} 型号规格`}
      className="flex flex-wrap gap-1.5"
    >
      {group.entries.map((variant) => (
        <button
          key={variant.id}
          type="button"
          title={
            Object.entries(variant.model_variants ?? {})
              .map(([key, value]) => `${key}=${value}`)
              .join(" · ") || "默认规格"
          }
          aria-pressed={variant.id === entry.id}
          onClick={() => onSelect(variant.id)}
          className={`min-w-7 rounded border px-2 py-1 font-mono text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${variant.id === entry.id ? "border-primary bg-primary/10 text-primary" : "border-border bg-background text-muted-foreground hover:bg-muted hover:text-foreground"}`}
        >
          {variantLabel(variant)}
        </button>
      ))}
    </div>
  );
}

function ClassSummary({ entry }: { entry: CatalogEntry }) {
  return (
    <>
      <p>{classCountLabel(entry)}</p>
      <p className="mt-1 text-muted-foreground">{classSourceLabel(entry)}</p>
    </>
  );
}

function classCountLabel(entry: CatalogEntry) {
  return entry.class_metadata.class_source === "request_prompt"
    ? "按提示确定"
    : `${entry.capabilities.classes?.length ?? 0} 类`;
}

function usageLabel(entry: CatalogEntry) {
  const usage = entry.operations.preannotate;
  return usage.allowed ? "可用于预标注 / 管线" : (usage.reason ?? "当前不可用");
}

function deploymentLabel(entry: CatalogEntry) {
  return (entry.placements ?? []).map((placement) => placement.name).join("、") || "尚无部署实例";
}

function UsageSummary({ entry }: { entry: CatalogEntry }) {
  const usage = entry.operations.preannotate;
  return (
    <>
      {entry.source === "project_trained" && (
        <Badge variant={entry.publication === "current" ? "success" : "default"} className="mb-1">
          {publicationLabels[entry.publication]}
        </Badge>
      )}
      <p className={usage.allowed ? "text-status-positive" : "text-muted-foreground"}>
        {usageLabel(entry)}
      </p>
      <p className="mt-1 text-muted-foreground">{deploymentLabel(entry)}</p>
    </>
  );
}

function ModelActions({ entry, projectId }: { entry: CatalogEntry; projectId: string }) {
  const project = entry.project?.id ?? projectId;
  return (
    <>
      {entry.project && entry.version_id && (
        <Link
          className="text-primary hover:underline"
          to={`/projects/${entry.project.id}/learning/models/${entry.version_id}`}
        >
          加载与试用
        </Link>
      )}
      {entry.operations.preannotate.allowed && project && isImageDetectionEntry(entry) && (
        <Link
          className="text-primary hover:underline"
          to={`/ai-pre?project=${encodeURIComponent(project)}&catalog_model=${encodeURIComponent(entry.id)}`}
        >
          用于预标注 / 管线
        </Link>
      )}
    </>
  );
}
