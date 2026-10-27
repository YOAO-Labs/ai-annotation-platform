import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { cn } from "@/lib/utils";
import { trainingOriginLabel, trainingValueLabel } from "./TrainingParameterField";
import type { ParameterValue } from "./trainingConfiguration";
import { trainingTaskLabel } from "./trainingPresentation";

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function TrainingRunReview({
  schema,
  values,
  config,
  digest,
}: {
  schema: TrainingParameterSchema;
  values: Record<string, ParameterValue>;
  config?: Record<string, unknown>;
  digest?: string;
}) {
  const resolved = record(config?.configuration);
  const effective = record(resolved.effective_values);
  const origins = record(resolved.origins);
  const inactive = record(resolved.inactive_fields);
  const base = record(config?.base_model);
  const initialization = record(config?.initialization);
  const weights = record(initialization.artifact);
  return (
    <div className="min-w-0 space-y-5">
      <div
        className={`rounded-lg p-4 text-sm ${config ? "bg-status-positive-soft text-status-positive" : "bg-muted text-muted-foreground"}`}
      >
        <p className="font-medium">
          {config ? "训练检查通过 · 请核对最终配置" : "尚未检查训练配置"}
        </p>
        <p className="mt-1 text-xs leading-relaxed">
          {config
            ? "下列数值来自服务端预检，包含默认值和依赖调整。开始训练将冻结这份配置。"
            : "当前显示自行设置值与受控默认值。检查后才能看到依赖调整与最终生效值。"}
        </p>
      </div>
      {config && (
        <details className="rounded-lg border border-border p-4 text-xs">
          <summary className="cursor-pointer font-medium">冻结的数据与运行环境</summary>
          <dl className="mt-3 grid gap-2 break-all text-muted-foreground">
            {[
              ["配置摘要", <CopyValueButton value={digest} label="复制训练配置摘要" />],
              [
                "数据版本",
                <CopyValueButton
                  value={String(config.dataset_id ?? "")}
                  kind="id"
                  label="复制训练数据版本 ID"
                />,
              ],
              ["训练任务", trainingTaskLabel(schema.task)],
              ["数据桥接", String(record(config.data_contract).bridge ?? "矩形框检测")],
              [
                "训练包 SHA256",
                <CopyValueButton
                  value={String(config.dataset_sha256 ?? "")}
                  label="复制训练包哈希"
                />,
              ],
              [
                "验证集摘要",
                <CopyValueButton
                  value={String(config.validation_digest ?? "")}
                  label="复制验证集摘要"
                />,
              ],
              ["类别", Array.isArray(config.classes) ? config.classes.join("、") : ""],
              ["基础模型", String(base.id ?? "—")],
              [
                "权重 SHA256",
                <CopyValueButton value={String(base.sha256 ?? "")} label="复制基础模型哈希" />,
              ],
              [
                "训练镜像",
                <CopyValueButton
                  value={String(config.image_digest ?? "")}
                  label="复制训练镜像摘要"
                />,
              ],
            ].map(([label, value]) => (
              <div key={String(label)} className="flex min-w-0 items-center gap-2">
                <dt className="shrink-0 text-foreground">{String(label)}</dt>
                <dd className="min-w-0">{value ?? "—"}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {initialization.kind === "trained_model" && (
        <section
          aria-label="冻结的初始化权重"
          className="space-y-3 rounded-lg border border-border p-4 text-xs"
        >
          <h3 className="font-medium">项目模型权重初始化 · 全新实验</h3>
          <p className="leading-relaxed text-muted-foreground">
            仅转移权重；从第 0 轮开始，新建优化器、调度器、缩放器、EMA
            和最佳成绩。本实验的断点续训使用自己的检查点。
          </p>
          <dl className="grid gap-2 break-all">
            {[
              ["候选模型", String(initialization.model_name ?? "—")],
              [
                "模型版本",
                <CopyValueButton
                  value={String(initialization.model_version_id ?? "")}
                  kind="id"
                  label="复制初始化模型版本 ID"
                />,
              ],
              [
                "来源实验",
                <CopyValueButton
                  value={String(initialization.run_id ?? "")}
                  kind="id"
                  label="复制来源训练实验 ID"
                />,
              ],
              [
                "来源执行尝试",
                <CopyValueButton
                  value={String(initialization.attempt_id ?? "")}
                  kind="id"
                  label="复制来源执行尝试 ID"
                />,
              ],
              [
                "来源摘要",
                <CopyValueButton
                  value={String(initialization.source_digest ?? "")}
                  label="复制初始化来源摘要"
                />,
              ],
              [
                "初始化权重 SHA256",
                <CopyValueButton value={String(weights.sha256 ?? "")} label="复制初始化权重哈希" />,
              ],
              ["权重字节数", String(weights.size_bytes ?? "—")],
              [
                "来源验证集摘要",
                <CopyValueButton
                  value={String(initialization.validation_digest ?? "")}
                  label="复制来源验证集摘要"
                />,
              ],
            ].map(([label, value]) => (
              <div key={String(label)} className="flex min-w-0 items-center gap-2">
                <dt className="shrink-0 text-muted-foreground">{String(label)}</dt>
                <dd className="min-w-0">{value ?? "—"}</dd>
              </div>
            ))}
          </dl>
        </section>
      )}
      {Object.entries(schema.groups).map(([group, label]) => {
        const fields = schema.fields.filter(
          (field) => field.group === group && field.status === "editable",
        );
        if (!fields.length) return null;
        const fieldValue = (field: (typeof fields)[number]) =>
          config ? effective[field.key] : field.key in values ? values[field.key] : field.default;
        const changed = (field: (typeof fields)[number]) =>
          JSON.stringify(fieldValue(field)) !== JSON.stringify(field.default);
        const changedCount = fields.filter(changed).length;
        const renderFields = (items: typeof fields) =>
          items.map((field) => {
            const value = fieldValue(field);
            const modified = changed(field);
            const origin = config
              ? String(origins[field.key] ?? "")
              : field.key in values
                ? "user"
                : "default";
            return (
              <div
                key={field.key}
                className={cn(
                  "grid min-w-0 gap-1 py-3 text-xs sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] sm:gap-3",
                  modified && "-mx-4 bg-status-info-soft px-4",
                )}
              >
                <div>
                  <p className={cn("font-medium", modified && "text-status-info")}>{field.label}</p>
                  <code className="break-all text-muted-foreground">{field.key}</code>
                </div>
                <div className="min-w-0 sm:text-right">
                  <p className={cn("break-all font-mono text-sm", modified && "text-status-info")}>
                    {trainingValueLabel(value)}
                  </p>
                  {modified && (
                    <p className="mt-1 text-muted-foreground">
                      默认：{trainingValueLabel(field.default)}
                    </p>
                  )}
                  <p className="mt-1 text-muted-foreground">{trainingOriginLabel(origin)}</p>
                  {field.key in inactive && (
                    <p className="mt-1 break-words text-status-caution">
                      当前不生效：{String(inactive[field.key])}
                    </p>
                  )}
                </div>
              </div>
            );
          });
        return (
          <details
            key={`${group}:${digest ?? "draft"}`}
            open={changedCount > 0}
            className="min-w-0 overflow-hidden rounded-lg border border-border bg-card"
          >
            <summary
              className={cn(
                "cursor-pointer px-4 py-3 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                changedCount > 0 && "bg-status-info-soft text-status-info",
              )}
            >
              {label}
              <span
                className={cn("ml-2 text-xs font-normal", !changedCount && "text-muted-foreground")}
              >
                {fields.length} 项 · {changedCount ? `${changedCount} 项有变化` : "均为默认值"}
              </span>
            </summary>
            <div className="divide-y divide-border px-4">
              {renderFields(changedCount ? fields.filter(changed) : fields)}
              {changedCount > 0 && changedCount < fields.length && (
                <details className="text-xs">
                  <summary className="cursor-pointer py-3 text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    {fields.length - changedCount} 项默认参数
                  </summary>
                  <div className="divide-y divide-border">
                    {renderFields(fields.filter((field) => !changed(field)))}
                  </div>
                </details>
              )}
            </div>
          </details>
        );
      })}
      <details className="rounded-lg border border-border p-4 text-xs">
        <summary className="cursor-pointer font-medium">平台管理参数 · 只读</summary>
        <p className="mt-2 leading-relaxed text-muted-foreground">
          设备、数据路径、保存位置和续训等选项由平台管理。此处列出目录中的固定值，运行时路径会在执行器内生成。
        </p>
        <dl className="mt-3 grid gap-2">
          {schema.fields
            .filter((field) => field.status === "platform-managed")
            .map((field) => (
              <div key={field.key} className="flex min-w-0 justify-between gap-3">
                <dt className="break-all text-muted-foreground">{field.key}</dt>
                <dd className="break-all text-right font-mono">
                  {trainingValueLabel(
                    field.key in effective ? effective[field.key] : field.default,
                  )}
                </dd>
              </div>
            ))}
        </dl>
      </details>
    </div>
  );
}
