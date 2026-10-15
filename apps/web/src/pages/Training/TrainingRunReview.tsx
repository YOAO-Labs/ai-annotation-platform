import type { TrainingParameterSchema } from "@/api/generated/types.gen";
import { trainingOriginLabel, trainingValueLabel } from "./TrainingParameterField";
import type { ParameterValue } from "./trainingConfiguration";

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
              ["配置摘要", digest],
              ["数据版本", config.dataset_id],
              ["训练包 SHA256", config.dataset_sha256],
              ["验证集摘要", config.validation_digest],
              ["类别", Array.isArray(config.classes) ? config.classes.join("、") : ""],
              ["基础权重", base.id],
              ["权重 SHA256", base.sha256],
              ["训练镜像", config.image_digest],
            ].map(([label, value]) => (
              <div key={String(label)}>
                <dt className="text-foreground">{String(label)}</dt>
                <dd className="mt-0.5 font-mono">{String(value ?? "—")}</dd>
              </div>
            ))}
          </dl>
        </details>
      )}
      {Object.entries(schema.groups).map(([group, label]) => {
        const fields = schema.fields.filter(
          (field) => field.group === group && field.status === "editable",
        );
        if (!fields.length) return null;
        return (
          <section key={group} className="min-w-0 rounded-lg border border-border bg-card">
            <h3 className="border-b border-border px-4 py-3 text-sm font-medium">{label}</h3>
            <div className="divide-y divide-border px-4">
              {fields.map((field) => {
                const value = config
                  ? effective[field.key]
                  : field.key in values
                    ? values[field.key]
                    : field.default;
                const origin = config
                  ? String(origins[field.key] ?? "")
                  : field.key in values
                    ? "user"
                    : "default";
                return (
                  <div
                    key={field.key}
                    className="grid min-w-0 gap-1 py-3 text-xs sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] sm:gap-3"
                  >
                    <div>
                      <p className="font-medium">{field.label}</p>
                      <code className="break-all text-muted-foreground">{field.key}</code>
                    </div>
                    <div className="min-w-0 sm:text-right">
                      <p className="break-all font-mono text-sm">{trainingValueLabel(value)}</p>
                      <p className="mt-1 text-muted-foreground">{trainingOriginLabel(origin)}</p>
                      {field.key in inactive && (
                        <p className="mt-1 break-words text-status-caution">
                          当前不生效：{String(inactive[field.key])}
                        </p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
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
