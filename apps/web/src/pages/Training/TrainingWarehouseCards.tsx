import { Box, Check, ChevronDown } from "lucide-react";
import type { TrainingHubCatalog } from "@/api/trainingHub";
import { Badge } from "@/components/ui/Badge";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { cn } from "@/lib/utils";
import { trainingTaskLabel } from "./trainingPresentation";

export function TrainingWarehouseCards({
  catalog,
  compact = false,
}: {
  catalog: TrainingHubCatalog;
  compact?: boolean;
}) {
  return (
    <div
      className={cn(compact ? "divide-y divide-border" : "grid items-start gap-4 xl:grid-cols-2")}
    >
      {catalog.items.map((item) => {
        const name = item.framework === "libreyolo" ? "LibreYOLO" : "Ultralytics";
        return (
          <article
            key={item.framework}
            className={cn(
              "min-w-0 p-4",
              !compact && "rounded-xl border border-border bg-card sm:p-5",
            )}
          >
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2.5">
                <Box aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                <div>
                  <h3 className="text-sm font-semibold">{name}</h3>
                  <p className="mt-1 font-mono text-2xs text-muted-foreground">{item.version}</p>
                </div>
              </div>
              <Badge
                dot
                variant={
                  !catalog.enabled
                    ? "default"
                    : item.compatible === false
                      ? "danger"
                      : catalog.online
                        ? catalog.busy
                          ? "warning"
                          : "success"
                        : "default"
                }
              >
                {!catalog.enabled
                  ? "未启用"
                  : item.compatible === false
                    ? "环境不匹配"
                    : catalog.online
                      ? catalog.busy
                        ? "资源使用中"
                        : "在线可用"
                      : "离线 · 可排队"}
              </Badge>
            </div>
            <ul className="mt-4 space-y-3" aria-label={`${name} 基础模型`}>
              {item.base_models.map((model) => (
                <li
                  key={model.id}
                  className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 text-xs"
                >
                  <div className="min-w-0">
                    <p className="break-all font-mono font-medium">{model.id}</p>
                    <p className="mt-1 text-muted-foreground">{trainingTaskLabel(model.task)}</p>
                  </div>
                  <span className="text-2xs text-muted-foreground">
                    {(model.capabilities?.model_registration ??
                    ((model.task ?? "detect") === "detect" && item.capabilities.model_registration))
                      ? "支持模型回流"
                      : "权重下载"}
                  </span>
                </li>
              ))}
            </ul>
            {!compact && (
              <>
                {(item.parameter_schemas ?? []).length > 0 && (
                  <dl className="mt-5 space-y-3 border-t border-border pt-4 text-xs">
                    {item.parameter_schemas?.map((schema) => (
                      <div key={schema.schema_id}>
                        <dt className="font-medium">
                          {schema.model_family} · {trainingTaskLabel(schema.task)}
                        </dt>
                        <dd className="mt-1 tabular-nums text-muted-foreground">
                          {schema.field_count} 个原生参数 / {schema.editable_count} 个可编辑
                        </dd>
                        <dd className="mt-1 text-2xs text-muted-foreground">
                          {schema.compatible === true
                            ? "执行器支持此参数契约"
                            : schema.compatible === false
                              ? "执行器需更新后才能使用扩展参数"
                              : "等待执行器上线确认"}{" "}
                          · 完整目录位于项目内
                        </dd>
                      </div>
                    ))}
                  </dl>
                )}
                {item.capabilities.resume && (
                  <p className="mt-4 flex items-center gap-1.5 text-xs text-muted-foreground">
                    <Check aria-hidden="true" className="size-3.5" />
                    支持检查点续训
                  </p>
                )}
                <details className="group mt-4 border-t border-border pt-3">
                  <summary className="flex cursor-pointer list-none items-center justify-between rounded-sm py-1 text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
                    {name} 技术信息
                    <ChevronDown
                      aria-hidden="true"
                      className="size-3.5 transition-transform group-open:rotate-180 motion-reduce:transition-none"
                    />
                  </summary>
                  <dl className="mt-3 space-y-4 text-xs">
                    <div className="flex items-center gap-1">
                      <dt className="text-muted-foreground">容器镜像摘要</dt>
                      <dd>
                        <CopyValueButton value={item.image_digest} label="复制容器镜像摘要" />
                      </dd>
                    </div>
                    <div>
                      <dt className="text-muted-foreground">基础权重</dt>
                      {item.base_models.map((model) => (
                        <dd key={model.id} className="mt-2">
                          <p className="flex min-w-0 items-center gap-1">
                            <span className="break-all font-medium">{model.filename}</span>
                            <CopyValueButton value={model.sha256} label="复制基础权重哈希" />
                          </p>
                        </dd>
                      ))}
                    </div>
                  </dl>
                </details>
              </>
            )}
            {item.compatible === false && (
              <p className="mt-3 text-xs leading-relaxed text-status-danger">
                在线执行器的镜像或框架版本与仓库配置不一致，请联系管理员更新执行器。
              </p>
            )}
          </article>
        );
      })}
    </div>
  );
}
