import { Badge } from "@/components/ui/Badge";
import type { FlatModel } from "./types";
import {
  buildListRows,
  currentPoolSize,
  effectiveInfra,
  effectiveModalities,
  isLoadedRuntimeKey,
} from "./catalogModel";
import { infraLabel, modalityLabel, taskLabel } from "./labels";
import { WarmButton } from "./WarmButton";

const TABLE_CLASS =
  "w-full min-w-[1080px] table-fixed border-separate border-spacing-0 text-xs " +
  "[&_td]:border-b [&_td]:border-border [&_td]:px-2.5 [&_td]:py-2 [&_td]:text-left [&_td]:align-middle " +
  "[&_th]:border-b [&_th]:border-border [&_th]:bg-muted [&_th]:px-2.5 [&_th]:py-2 [&_th]:text-left [&_th]:align-middle " +
  "[&_th]:whitespace-nowrap [&_th]:text-xs [&_th]:font-semibold [&_th]:text-muted-foreground";

const COLUMN_CLASSES = [
  "w-[20%]",
  "w-[6%]",
  "w-[6%]",
  "w-[6%]",
  "w-[8%]",
  "w-[9%]",
  "w-[14%]",
  "w-[14%]",
  "w-[10%]",
  "w-[7%]",
];
const TRUNCATE_CLASS = "block max-w-full overflow-hidden text-ellipsis whitespace-nowrap";

export function ModelListTable({ items }: { items: FlatModel[] }) {
  const rows = buildListRows(items);
  return (
    <div className="max-w-full overflow-x-auto">
      <table className={TABLE_CLASS}>
        <colgroup>
          {COLUMN_CLASSES.map((className, index) => (
            <col key={index} className={className} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {[
              "模型",
              "任务",
              "推理框架",
              "模态",
              "输出几何",
              "变体",
              "模型状态",
              "来源",
              "项目启用",
              "目录信息",
            ].map((head) => (
              <th key={head}>{head}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const item = row.parent;
            const m = item.model;
            const infra = effectiveInfra(m, item.backendInfra);
            const modalities = effectiveModalities(m, item.backendModalities);
            return (
              <tr key={row.rowKey}>
                <td className="min-w-[180px]">
                  <span className={`${TRUNCATE_CLASS} font-semibold`} title={row.primaryLabel}>
                    {row.primaryLabel}
                  </span>
                  <div
                    className={`${TRUNCATE_CLASS} mono mt-0.5 text-2xs text-muted-foreground`}
                    title={row.primaryId}
                  >
                    {row.primaryId}
                  </div>
                </td>
                <td className="text-muted-foreground">
                  {row.tasks.length > 0 ? row.tasks.map(taskLabel).join(" / ") : "—"}
                </td>
                <td>{infra ? infraLabel(infra) : "—"}</td>
                <td>{modalities.length ? modalities.map(modalityLabel).join(" / ") : "—"}</td>
                <td className="text-muted-foreground">
                  <span className={TRUNCATE_CLASS} title={row.geometries.join(" / ")}>
                    {row.geometries.join(" / ") || "—"}
                  </span>
                </td>
                <td className="text-muted-foreground" title={row.secondaryTitle}>
                  <span className={TRUNCATE_CLASS}>{row.secondaryLabel}</span>
                </td>
                <td>
                  <RuntimeCell
                    item={item}
                    variants={row.warmVariants}
                    runtimeKey={row.runtimeKey}
                  />
                </td>
                <td className="text-muted-foreground">
                  <span className={TRUNCATE_CLASS} title={item.backendName}>
                    {item.backendName}
                  </span>
                </td>
                <td
                  className="text-muted-foreground"
                  title={
                    item.source === "registered" && item.registeredProjects.length > 0
                      ? `已注册至项目: ${item.registeredProjects.join(" / ")}`
                      : undefined
                  }
                >
                  {item.source === "env_only" ? (
                    <Badge variant="outline">平台内置</Badge>
                  ) : item.registeredProjects.length > 1 ? (
                    <Badge variant="accent">
                      {item.registeredProjects[0]} +{item.registeredProjects.length - 1}
                    </Badge>
                  ) : (
                    <Badge variant="accent">{item.projectName}</Badge>
                  )}
                </td>
                <td>
                  {item.stale ? (
                    <Badge variant="warning">可能使用缓存</Badge>
                  ) : (
                    <Badge variant="outline">已读取</Badge>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RuntimeCell({
  item,
  variants,
  runtimeKey,
}: {
  item: FlatModel;
  variants: Record<string, string>;
  runtimeKey?: string;
}) {
  const loaded = isLoadedRuntimeKey(item, variants, runtimeKey);
  return (
    <div className="flex min-w-0 items-center gap-1.5 whitespace-nowrap">
      <span className="whitespace-nowrap text-xs text-muted-foreground">
        池 {currentPoolSize(item)}
      </span>
      {item.healthMeta?.pool ? (
        <Badge variant={loaded ? "success" : "outline"}>{loaded ? "已加载" : "未加载"}</Badge>
      ) : (
        <span className="text-xs text-muted-foreground">运行状态未知</span>
      )}
      <WarmButton item={item} variants={variants} compact size="xs" />
    </div>
  );
}
