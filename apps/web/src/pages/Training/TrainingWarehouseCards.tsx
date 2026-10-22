import { ListPagination } from "@/components/ui/ListPagination";
import { useListPagination } from "@/hooks/useListPagination";
import { Box, Check } from "lucide-react";
import type { TrainingHubCatalog } from "@/api/trainingHub";
import { Badge } from "@/components/ui/Badge";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { cn } from "@/lib/utils";
import {
  trainingModelFamily,
  trainingModelFamilyLabels,
  trainingTaskLabel,
} from "./trainingPresentation";

type Warehouse = TrainingHubCatalog["items"][number];
type BaseModel = Warehouse["base_models"][number];

function supportsRegistration(model: BaseModel, warehouse: Warehouse) {
  return (
    model.capabilities?.model_registration ??
    ((model.task ?? "detect") === "detect" && warehouse.capabilities.model_registration)
  );
}

function groupModels(warehouse: Warehouse) {
  const groups = new Map<
    string,
    {
      family: string;
      task: string;
      models: BaseModel[];
      schemas: NonNullable<Warehouse["parameter_schemas"]>;
    }
  >();
  const getGroup = (family: string, task: string) => {
    const key = `${family}:${task}`;
    let group = groups.get(key);
    if (!group) {
      group = { family, task, models: [], schemas: [] };
      groups.set(key, group);
    }
    return group;
  };
  for (const model of warehouse.base_models) {
    const family = trainingModelFamily(model);
    const task = model.task ?? "detect";
    getGroup(family, task).models.push(model);
  }
  for (const schema of warehouse.parameter_schemas ?? []) {
    getGroup(schema.model_family, schema.task).schemas.push(schema);
  }
  return Array.from(groups.values());
}

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
      {catalog.items.map((item) => (
        <WarehouseCard key={item.framework} item={item} compact={compact} catalog={catalog} />
      ))}
    </div>
  );
}

function WarehouseCard({
  item,
  compact,
  catalog,
}: {
  item: Warehouse;
  compact: boolean;
  catalog: TrainingHubCatalog;
}) {
  const name = item.framework === "libreyolo" ? "LibreYOLO" : "Ultralytics";
  const modelGroups = compact ? [] : groupModels(item);
  const pagination = useListPagination(modelGroups, item.framework);
  return (
    <article
      key={item.framework}
      className={cn("min-w-0 p-4", !compact && "rounded-xl border border-border bg-card sm:p-5")}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2.5">
          <Box aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
          <div>
            <h3 className="text-sm font-semibold">{name}</h3>
            <div className="mt-1 flex items-center gap-1 font-mono text-2xs text-muted-foreground">
              <span>{item.version}</span>
              {!compact && <CopyValueButton value={item.image_digest} label="复制容器镜像摘要" />}
            </div>
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
      {!compact && (
        <>
          {modelGroups.length > 0 ? (
            <div className="mt-4">
              <p className="mb-2 flex items-baseline gap-2 text-xs font-medium">
                基础模型与训练参数
                <span className="font-normal tabular-nums text-muted-foreground">
                  {item.base_models.length} 个 · {modelGroups.length} 组
                </span>
              </p>
              <ul
                className="divide-y divide-border border-y border-border"
                aria-label={`${name} 基础模型`}
              >
                {pagination.items.map((group) => {
                  const registrations = group.models.map((model) =>
                    supportsRegistration(model, item),
                  );
                  const mixedCapabilities = new Set(registrations).size > 1;
                  return (
                    <li key={`${group.family}:${group.task}`} className="py-3 text-xs">
                      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                        <p className="min-w-0 break-words font-medium">
                          {trainingModelFamilyLabels[group.family] ?? group.family}
                          <span className="ml-2 font-normal text-muted-foreground">
                            {trainingTaskLabel(group.task)}
                          </span>
                        </p>
                        {group.models.length > 0 && !mixedCapabilities && (
                          <span className="text-2xs text-muted-foreground">
                            {registrations[0] ? "支持模型回流" : "权重下载"}
                          </span>
                        )}
                      </div>
                      <div className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
                        <div className="flex min-w-0 flex-wrap gap-1">
                          {group.models.map((model, index) => {
                            const capability = registrations[index] ? "支持模型回流" : "权重下载";
                            return (
                              <span
                                key={model.id}
                                title={`${model.id} · ${capability}`}
                                aria-label={`${model.id} · ${capability}`}
                                className="inline-flex items-center gap-1 rounded bg-muted pl-1.5 text-2xs"
                              >
                                <span className="break-all font-mono" title={model.filename}>
                                  {model.model_size ?? model.id}
                                </span>
                                {mixedCapabilities && (
                                  <span className="text-muted-foreground">
                                    {registrations[index] ? "可回流" : "下载"}
                                  </span>
                                )}
                                <CopyValueButton
                                  value={model.sha256}
                                  label={`复制 ${model.id} 基础权重哈希`}
                                />
                              </span>
                            );
                          })}
                          {group.models.length === 0 && (
                            <span className="text-2xs text-muted-foreground">尚未配置基础模型</span>
                          )}
                        </div>
                        {group.schemas.map((schema) => (
                          <p
                            key={schema.schema_id}
                            className="text-2xs tabular-nums text-muted-foreground"
                          >
                            {schema.field_count} 个原生参数 / {schema.editable_count} 个可编辑
                            <span
                              className={cn(
                                "ml-2",
                                schema.compatible === false && "text-status-danger",
                              )}
                            >
                              {schema.compatible === true
                                ? "已兼容"
                                : schema.compatible === false
                                  ? "执行器需更新"
                                  : "待执行器确认"}
                            </span>
                          </p>
                        ))}
                      </div>
                    </li>
                  );
                })}
              </ul>
              <ListPagination
                label={`${name} 模型组`}
                page={pagination.page}
                pageSize={pagination.pageSize}
                total={pagination.total}
                onPageChange={pagination.setPage}
                onPageSizeChange={pagination.setPageSize}
              />
              <p className="mt-3 text-2xs text-muted-foreground">
                规格旁可复制基础权重校验值，完整参数目录位于项目内。
              </p>
            </div>
          ) : (
            <p className="mt-4 text-xs text-muted-foreground">尚未配置基础模型</p>
          )}
          {item.capabilities.resume && (
            <p className="mt-4 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Check aria-hidden="true" className="size-3.5" />
              支持检查点续训
            </p>
          )}
        </>
      )}
      {item.compatible === false && (
        <p className="mt-3 text-xs leading-relaxed text-status-danger">
          在线执行器的镜像或框架版本与仓库配置不一致，请联系管理员更新执行器。
        </p>
      )}
    </article>
  );
}
