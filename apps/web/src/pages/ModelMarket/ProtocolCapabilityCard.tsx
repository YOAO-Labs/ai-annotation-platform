// v0.14.11 · 协议能力卡片 — 一张卡 = 一个协议 task; 卡内挂载已注册 backend 的 model.
// 卡内已接入模型直接复用 ModelCard, 与 groupBy=backend/infra 等其他分组视图字段对齐;
// 空态仍引导注册。
//
// 模型市场多 TAB UI 优化 · 阶段二 (plan §4.1)：任务标签已在卡标题呈现, 不在卡内
// 重复徽标; 未接入能力首层只保留一行说明 + 接入入口, 推荐后端收进展开区域。
import { useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { ProtocolTask } from "@/api/mlCapabilities";
import { ModelCard } from "./capability/ModelCard";
import type { FlatModel } from "./capability/types";

interface Props {
  task: ProtocolTask;
  mounted: FlatModel[];
  /** 中文 infra label (复用 panel 内 INFRA_LABELS, 通过 props 注入避免循环依赖). */
  infraLabel: (infra: string) => string;
  /** 中文 modality label. */
  modalityLabel: (modality: string) => string;
  /** 点 「去注册」按钮的回调 (跳 ?tab=registry). */
  onGoToRegistry?: () => void;
}

export function ProtocolCapabilityCard({
  task,
  mounted,
  infraLabel,
  modalityLabel,
  onGoToRegistry,
}: Props) {
  const [suggestOpen, setSuggestOpen] = useState(false);
  const empty = mounted.length === 0;
  const suggested = [...task.suggested_backends]
    .sort((a, b) => Number(b.builtin) - Number(a.builtin))
    .slice(0, 4);

  return (
    <div
      className={`flex flex-col gap-2.5 rounded-md border px-4 py-3.5 ${
        empty ? "border-dashed border-border bg-muted" : "border-border bg-card"
      }`}
    >
      <div className="flex flex-wrap items-center gap-2.5">
        <div className="flex min-w-0 flex-auto items-baseline gap-2">
          <h3 className="m-0 text-sm font-semibold text-foreground">{task.label}</h3>
          <span className="mono text-xs text-muted-foreground">{task.id}</span>
        </div>
        <div className="flex flex-wrap gap-1.5">
          {task.default_modalities.map((m) => (
            <Badge key={m} variant="default">
              {modalityLabel(m)}
            </Badge>
          ))}
          {task.default_geometry.map((g) => (
            <Badge key={g} variant="outline">
              {g}
            </Badge>
          ))}
          {empty ? (
            <Badge variant="outline">暂无接入</Badge>
          ) : (
            <Badge variant="success">{mounted.length} 个模型已接入</Badge>
          )}
        </div>
      </div>

      <p className="m-0 text-xs leading-normal text-muted-foreground">{task.summary}</p>

      {!empty && (
        <div className="mt-1 grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-2">
          {mounted.map((m) => (
            <ModelCard
              key={`${m.backendId}:${m.model.id}`}
              item={m}
              // 任务标签已在卡标题呈现（plan §4.1），不在每张模型卡内重复。
              showTaskBadge={false}
            />
          ))}
        </div>
      )}

      {empty && (
        <div className="flex flex-col gap-2 border-t border-dashed border-border pt-2.5">
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>
              <strong>典型模型：</strong>
              {task.typical_models.join(" / ") || "—"}
            </span>
            {onGoToRegistry && (
              <Button size="sm" onClick={onGoToRegistry} className="ml-auto">
                <Icon name="plus" size={11} /> 去注册 backend
              </Button>
            )}
          </div>
          {suggested.length > 0 && (
            <div className="flex flex-col gap-1.5">
              <button
                type="button"
                className="flex w-fit cursor-pointer appearance-none items-center gap-1 border-0 bg-transparent p-0 text-xs text-muted-foreground hover:text-foreground"
                onClick={() => setSuggestOpen((v) => !v)}
                aria-expanded={suggestOpen}
              >
                <Icon name={suggestOpen ? "chevDown" : "chevRight"} size={11} />
                推荐后端（{suggested.length}）
              </button>
              {suggestOpen && (
                <div className="flex flex-col gap-1.5">
                  {suggested.map((s) => (
                    <div key={s.repo_url} className="flex items-center gap-2 text-xs">
                      {s.builtin && <Badge variant="success">自带</Badge>}
                      <span className="font-medium text-foreground">{s.name}</span>
                      {s.infra && <Badge variant="outline">{infraLabel(s.infra)}</Badge>}
                      <span className="flex-auto text-muted-foreground">{s.summary}</span>
                      <a
                        href={s.repo_url}
                        target="_blank"
                        rel="noreferrer"
                        className="text-xs text-brand no-underline hover:underline"
                        title="在新标签页打开仓库"
                      >
                        GitHub ↗
                      </a>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
