import { CopyableError } from "@/components/ui/CopyableError";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { trainingRunsApi } from "@/api/trainingRuns";
import { CopyValueButton } from "@/components/ui/CopyValueButton";

const statusLabels = {
  editable: "支持编辑",
  "platform-managed": "平台管理",
  inapplicable: "不适用于检测",
  unqualified: "尚未验证",
};
const inputClass =
  "w-full rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring";
const showValue = (value: unknown) => (value === null ? "未设置" : JSON.stringify(value));

export function TrainingParameterInventory({
  projectId,
  framework,
  baseModel,
}: {
  projectId: string;
  framework: string;
  baseModel: string;
}) {
  const [search, setSearch] = useState("");
  const [group, setGroup] = useState("");
  const [status, setStatus] = useState("");
  const schema = useQuery({
    queryKey: ["training-parameter-schema", projectId, framework, baseModel],
    queryFn: () => trainingRunsApi.parameterSchema(projectId, framework, baseModel),
    enabled: !!baseModel,
    staleTime: 60_000,
  });
  if (schema.isPending)
    return (
      <p role="status" className="text-xs text-muted-foreground">
        正在读取参数目录…
      </p>
    );
  if (schema.isError)
    return (
      <p role="alert" className="text-xs text-status-danger">
        <CopyableError message={schema.error.message} />
      </p>
    );
  const definition = schema.data;
  const needle = search.trim().toLocaleLowerCase();
  const fields = definition.fields.filter(
    (field) =>
      (!group || field.group === group) &&
      (!status || field.status === status) &&
      (!needle ||
        `${field.key} ${field.label} ${field.reason} ${field.aliases.join(" ")}`
          .toLocaleLowerCase()
          .includes(needle)),
  );
  return (
    <div className="space-y-3">
      <p className="text-xs leading-relaxed text-muted-foreground">
        {definition.framework} {definition.framework_version} · {definition.model_family} 检测 · 共{" "}
        {definition.fields.length} 项参数（含平台选项）。
        平台管理、不适用和未验证的参数不能通过自定义配置覆盖。
      </p>
      <div className="grid gap-2 sm:grid-cols-3">
        <input
          aria-label="搜索参数"
          placeholder="名称、原生键或别名"
          className={inputClass}
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <select
          aria-label="参数分组"
          className={inputClass}
          value={group}
          onChange={(event) => setGroup(event.target.value)}
        >
          <option value="">全部分组</option>
          {Object.entries(definition.groups).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
        <select
          aria-label="参数支持状态"
          className={inputClass}
          value={status}
          onChange={(event) => setStatus(event.target.value)}
        >
          <option value="">全部状态</option>
          {Object.entries(statusLabels).map(([key, label]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
      </div>
      <p role="status" className="text-xs text-muted-foreground">
        显示 {fields.length} / {definition.fields.length} 项
      </p>
      <div className="grid max-h-96 gap-2 overflow-y-auto sm:grid-cols-2">
        {fields.map((field) => (
          <article
            key={field.key}
            className="min-w-0 rounded-md border border-border bg-card p-3 text-xs"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-medium">{field.label}</span>
              <span className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground">
                {statusLabels[field.status]}
              </span>
            </div>
            <code className="mt-1 block break-all text-muted-foreground">
              {field.key}
              {field.aliases.length ? ` · 别名 ${field.aliases.join(", ")}` : ""}
            </code>
            <p className="mt-2 break-all">默认：{showValue(field.default)}</p>
            {JSON.stringify(field.default) !== JSON.stringify(field.native_default) && (
              <p className="mt-1 break-all text-muted-foreground">
                原生默认：{showValue(field.native_default)}
              </p>
            )}
            <p className="mt-2 leading-relaxed text-muted-foreground">{field.reason}</p>
            {field.status === "editable" && (
              <details className="mt-2 text-muted-foreground">
                <summary className="cursor-pointer">类型与允许范围</summary>
                <pre className="mt-1 whitespace-pre-wrap break-all text-xs">
                  {JSON.stringify(field.rules, null, 2)}
                </pre>
              </details>
            )}
          </article>
        ))}
      </div>
      {!fields.length && <p className="py-3 text-xs text-muted-foreground">没有匹配的参数。</p>}
      <details className="text-xs text-muted-foreground">
        <summary className="cursor-pointer">定义与原生来源</summary>
        <p className="mt-2 flex flex-wrap items-center gap-1">
          <span>{definition.schema_id} · 摘要</span>
          <CopyValueButton value={definition.schema_digest} label="复制参数目录摘要" />
        </p>
        <a
          className="mt-1 inline-block text-primary underline"
          href={definition.source}
          target="_blank"
          rel="noreferrer"
        >
          查看锁定版本的原生配置
        </a>
      </details>
    </div>
  );
}
