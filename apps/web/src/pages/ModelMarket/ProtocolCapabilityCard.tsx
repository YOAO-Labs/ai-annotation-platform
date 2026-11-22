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
  infraLabel: (infra: string) => string;
  modalityLabel: (modality: string) => string;
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
            <ModelCard key={`${m.backendId}:${m.model.id}`} item={m} showTaskBadge={false} />
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
                <Icon name="plus" size={11} /> 查看注册管理
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
                参考模型服务（{suggested.length}）
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
