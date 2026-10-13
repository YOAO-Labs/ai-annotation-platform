// 协议能力列表视图: groupBy=task 时与协议卡片共享同一份 protocolView 数据。
//
// 模型市场多 TAB UI 优化 · 阶段二 (plan §4.1)：行保持紧凑——能力、输入/输出、
// 接入数量、模型名称摘要和「查看全部 N 个模型」；该入口实际展开剩余模型（替换
// 旧不可点击的「+N 个模型」文字）。未接入能力显示一行说明和接入入口，推荐后端
// 在展开区域列出。
import { Fragment, useState, type ReactNode } from "react";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { ProtocolTask } from "@/api/mlCapabilities";
import { taskVariant } from "./capability/labels";
import { effectiveInfra } from "./capability/catalogModel";
import { ModelCard } from "./capability/ModelCard";
import type { FlatModel } from "./capability/types";

const TABLE_CLASS =
  "w-full min-w-[860px] border-separate border-spacing-0 text-xs " +
  "[&_td]:border-b [&_td]:border-border [&_td]:px-3 [&_td]:py-2.5 [&_td]:text-left [&_td]:align-top " +
  "[&_th]:border-b [&_th]:border-border [&_th]:bg-muted [&_th]:px-3 [&_th]:py-2 [&_th]:text-left [&_th]:align-top " +
  "[&_th]:whitespace-nowrap [&_th]:text-xs [&_th]:font-semibold [&_th]:text-muted-foreground";

interface ProtocolCapabilityListRow {
  task: ProtocolTask;
  mounted: FlatModel[];
}

interface Props {
  rows: ProtocolCapabilityListRow[];
  infraLabel: (infra: string) => string;
  modalityLabel: (modality: string) => string;
  onGoToRegistry?: () => void;
}

export function ProtocolCapabilityListTable({
  rows,
  infraLabel,
  modalityLabel,
  onGoToRegistry,
}: Props): ReactNode {
  const [expandedTasks, setExpandedTasks] = useState<Set<string>>(new Set());
  const toggleTask = (taskId: string) => {
    setExpandedTasks((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  return (
    <div className="max-w-full overflow-x-auto">
      <table className={TABLE_CLASS}>
        <thead>
          <tr>
            {["协议能力", "输入 / 输出", "接入状态", "已接入模型", "典型模型 / 推荐接入"].map(
              (head) => (
                <th key={head}>{head}</th>
              ),
            )}
          </tr>
        </thead>
        <tbody>
          {rows.map(({ task, mounted }) => {
            const suggested = [...task.suggested_backends]
              .sort((a, b) => Number(b.builtin) - Number(a.builtin))
              .slice(0, 2);
            const expanded = expandedTasks.has(task.id);
            const restCount = mounted.length - 3;
            return (
              <Fragment key={task.id}>
                <tr>
                  <td className="min-w-[200px]">
                    <div className="flex min-w-0 items-center gap-2">
                      <Badge variant={taskVariant(task.id)}>{task.label}</Badge>
                      <span className="mono text-2xs text-muted-foreground">{task.id}</span>
                    </div>
                    <div className="mt-1.5 max-w-[320px] text-xs leading-normal text-muted-foreground">
                      {task.summary}
                    </div>
                  </td>
                  {/* 输入/输出合并为一列：默认模态 → 默认几何。 */}
                  <td className="min-w-[150px]">
                    <div className="flex flex-col gap-1">
                      <div className="flex flex-wrap gap-1">
                        {task.default_modalities.length > 0 ? (
                          task.default_modalities.map((m) => (
                            <Badge key={m} variant="default">
                              {modalityLabel(m)}
                            </Badge>
                          ))
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </div>
                      <span className="text-2xs text-muted-foreground">
                        →{" "}
                        {task.default_geometry.length > 0 ? task.default_geometry.join(" · ") : "—"}
                      </span>
                    </div>
                  </td>
                  <td className="min-w-[110px]">
                    {mounted.length > 0 ? (
                      <Badge variant="success">{mounted.length} 个模型</Badge>
                    ) : (
                      <Badge variant="outline">暂无接入</Badge>
                    )}
                  </td>
                  <td className="min-w-[260px] max-w-[380px]">
                    {mounted.length > 0 ? (
                      <div className="flex flex-col gap-1.5">
                        {mounted.slice(0, 3).map((m) => {
                          const infra = effectiveInfra(m.model, m.backendInfra);
                          return (
                            <div
                              key={`${task.id}:${m.backendName}:${m.model.id}`}
                              className="min-w-0 rounded-sm border border-border bg-muted px-2 py-1.5"
                            >
                              <div
                                className="overflow-hidden text-ellipsis whitespace-nowrap font-semibold text-foreground"
                                title={m.model.display_name ?? m.model.id}
                              >
                                {m.model.display_name ?? m.model.id}
                              </div>
                              <div className="mt-0.5 flex min-w-0 flex-wrap items-center gap-1.5 text-2xs text-muted-foreground">
                                {infra && <span>{infraLabel(infra)}</span>}
                                {m.model.is_interactive && <span>交互式</span>}
                                <Badge variant={m.source === "env_only" ? "success" : "outline"}>
                                  {m.source === "env_only" ? "自带" : "已注册"}
                                </Badge>
                                <span
                                  className="inline-flex min-w-0 items-center gap-1 overflow-hidden text-ellipsis whitespace-nowrap"
                                  title={m.backendName}
                                >
                                  <Icon name="bot" size={10} /> {m.backendName}
                                </span>
                              </div>
                            </div>
                          );
                        })}
                        {restCount > 0 && (
                          <Button
                            size="xs"
                            variant="ghost"
                            className="w-fit"
                            onClick={() => toggleTask(task.id)}
                            aria-expanded={expanded}
                            title={`展开「${task.label}」全部 ${mounted.length} 个模型`}
                          >
                            <Icon name={expanded ? "chevDown" : "chevRight"} size={11} />
                            查看全部 {mounted.length} 个模型
                          </Button>
                        )}
                      </div>
                    ) : (
                      <span className="text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="min-w-[240px] max-w-[360px]">
                    <div className="flex flex-col gap-1.5">
                      <div className="text-muted-foreground">
                        {task.typical_models.length > 0 ? task.typical_models.join(" / ") : "—"}
                      </div>
                      {suggested.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {suggested.map((s) => (
                            <Badge key={s.repo_url} variant={s.builtin ? "success" : "outline"}>
                              {s.name}
                            </Badge>
                          ))}
                        </div>
                      )}
                      {mounted.length === 0 && onGoToRegistry && (
                        <div>
                          <Button size="sm" onClick={onGoToRegistry}>
                            <Icon name="plus" size={11} /> 去注册
                          </Button>
                        </div>
                      )}
                    </div>
                  </td>
                </tr>
                {/* 展开区：实际渲染全部（含剩余）模型的摘要卡。 */}
                {expanded && mounted.length > 3 && (
                  <tr className="bg-muted/20">
                    <td colSpan={5}>
                      <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-2">
                        {mounted.map((m) => (
                          <ModelCard
                            key={`exp:${m.backendId}:${m.model.id}`}
                            item={m}
                            showTaskBadge={false}
                          />
                        ))}
                      </div>
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
