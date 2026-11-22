import { useState, type ReactNode } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { FlatModel } from "./types";
import {
  currentPoolSize,
  effectiveInfra,
  effectiveModalities,
  formatEvict,
  isLoadedRuntimeKey,
  lastEvict,
  pickDefaultVariants,
  runtimeKeyFor,
  tierLabel,
} from "./catalogModel";
import {
  COMPOSITION_BADGE,
  infraLabel,
  inputLabel,
  modalityLabel,
  taskLabel,
  taskVariant,
} from "./labels";
import { WarmButton } from "./WarmButton";

const TAG_CLASS =
  "mono rounded-sm bg-muted px-2 py-px text-2xs leading-[1.5] text-muted-foreground";
const VARIANT_PILL_BASE =
  "inline-flex items-center rounded-full border px-2 py-px text-2xs leading-[1.6]";

export function ModelCard({
  item,
  showTaskBadge = true,
}: {
  item: FlatModel;
  showTaskBadge?: boolean;
}) {
  const { model: m } = item;
  const [detailOpen, setDetailOpen] = useState(false);
  const infra = effectiveInfra(m, item.backendInfra);
  const modalities = effectiveModalities(m, item.backendModalities);
  const geom = m.supported_geometric_outputs ?? [];
  const attrs =
    m.output_attribute_schema && m.output_attribute_schema.length > 0
      ? m.output_attribute_schema.map((s) => s.label || s.key)
      : (m.output_attribute_types ?? []);
  const variantGroups = (m.supported_variants ?? []).filter(
    (g) => Array.isArray(g.variants) && g.variants.length > 0,
  );
  const resource = m.resource_profile ?? {};
  const batchable = typeof resource.batchable === "boolean" ? resource.batchable : undefined;
  const device = typeof resource.device === "string" ? resource.device : undefined;
  const resourceEntries = Object.entries(resource).filter(
    ([k, v]) => v != null && k !== "batchable" && k !== "device",
  );
  const defaultVariants = pickDefaultVariants(m);
  const cardRuntimeKey = runtimeKeyFor(m.task, defaultVariants);
  const loaded = isLoadedRuntimeKey(item, defaultVariants, cardRuntimeKey);
  const evict = lastEvict(item);
  const comp = COMPOSITION_BADGE[m.composition ?? "atom"];
  const runtimeKnown = item.healthMeta?.pool != null;

  const inputLabels =
    (m.supported_inputs?.length ?? 0) > 0
      ? m.supported_inputs!.map((i) => inputLabel(i))
      : ["整图"];
  const outputParts = [...geom, ...(m.supported_text_outputs ?? [])];

  return (
    <div className="flex flex-col gap-2 rounded-md border border-border bg-card px-3.5 py-3">
      <div className="flex items-center justify-between gap-2">
        <span
          className="overflow-hidden text-ellipsis whitespace-nowrap text-sm font-semibold"
          title={m.display_name ?? m.id}
        >
          {m.display_name ?? m.id}
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {item.warnings && item.warnings.length > 0 && (
            <span
              title={item.warnings.map((w) => `${w.field}=${w.value}: ${w.message}`).join("\n")}
            >
              <Badge variant="warning">⚠ 协议 {item.warnings.length}</Badge>
            </span>
          )}
          {item.stale && (
            <span title="来源模型服务最近未连接，目录可能使用缓存；缓存不能证明当前连接状态">
              <Badge variant="warning">缓存</Badge>
            </span>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {showTaskBadge && m.task && (
          <Badge variant={taskVariant(m.task)}>{taskLabel(m.task)}</Badge>
        )}
        {comp && (
          <span title={comp.title}>
            <Badge variant={comp.variant}>{comp.label}</Badge>
          </span>
        )}
        {modalities.map((mod) => (
          <Badge key={mod} variant="default">
            {modalityLabel(mod)}
          </Badge>
        ))}
        {m.is_interactive && <Badge variant="ai">交互式</Badge>}
        {batchable === true && (
          <span title="resource_profile.batchable=true：可用于批量预标流水线">
            <Badge variant="success">可批量</Badge>
          </span>
        )}
        {batchable === false && (
          <span title="resource_profile.batchable=false：交互/有状态模型，不可用于批量预标">
            <Badge variant="warning">交互/有状态</Badge>
          </span>
        )}
        {device && (
          <span title={`resource_profile.device=${device}`}>
            <Badge variant="outline">{device.toUpperCase()}</Badge>
          </span>
        )}
      </div>

      <div
        className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
        title={`${item.projectName} · ${item.backendName}`}
      >
        <Icon name="bot" size={11} className="text-muted-foreground" />
        <span className="overflow-hidden text-ellipsis whitespace-nowrap">
          {item.projectName} · {item.backendName}
        </span>
      </div>

      <div
        className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground"
        title={`输入 ${inputLabels.join("、")} → 输出 ${outputParts.join("、") || "未知"}`}
      >
        <span className="shrink-0 text-2xs font-semibold">输入 → 输出</span>
        <span className="truncate">
          {inputLabels.join(" / ")} → {outputParts.length > 0 ? outputParts.join(" · ") : "—"}
        </span>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="w-14 shrink-0 text-2xs font-semibold text-muted-foreground">运行时</span>
        {runtimeKnown ? (
          <>
            <Badge variant="default">池 {currentPoolSize(item)}</Badge>
            <Badge variant={loaded ? "success" : "outline"}>{loaded ? "已加载" : "未加载"}</Badge>
          </>
        ) : (
          <span className="text-2xs text-muted-foreground">运行状态未知</span>
        )}
        <WarmButton item={item} variants={defaultVariants} size="xs" />
      </div>

      <div className="flex items-center justify-end border-t border-dashed border-border pt-1.5">
        <Button
          size="xs"
          variant="ghost"
          onClick={() => setDetailOpen((v) => !v)}
          aria-expanded={detailOpen}
          title={detailOpen ? "收起详细合同" : "展开详细合同与变体"}
        >
          <Icon name={detailOpen ? "chevDown" : "chevRight"} size={11} />
          {detailOpen ? "收起详情" : "详情"}
        </Button>
      </div>

      {detailOpen && (
        <div className="flex flex-col gap-2">
          <Row label="推理框架">
            {infra ? (
              <span className={TAG_CLASS}>{infraLabel(infra)}</span>
            ) : (
              <span className={TAG_CLASS}>—</span>
            )}
          </Row>
          {m.model_family && (
            <Row label="模型族">
              <span className="mono rounded-full border border-border px-2 py-px text-2xs text-muted-foreground">
                {m.model_family}
              </span>
            </Row>
          )}
          <Row label="可接受输入">
            {(m.supported_inputs?.length ?? 0) > 0 ? (
              m.supported_inputs!.map((i) => (
                <span key={i} className={TAG_CLASS}>
                  {inputLabel(i)}
                </span>
              ))
            ) : (
              <span className={TAG_CLASS}>整图</span>
            )}
          </Row>

          <Row label="输出几何">
            {geom.length > 0 ? (
              geom.map((g) => (
                <span key={g} className={TAG_CLASS}>
                  {g}
                </span>
              ))
            ) : (
              <span className={TAG_CLASS}>—</span>
            )}
          </Row>

          <Row label="输出属性">
            {attrs.length > 0 ? (
              attrs.map((a) => (
                <span key={a} className={TAG_CLASS}>
                  {a}
                </span>
              ))
            ) : (
              <span className={TAG_CLASS}>—</span>
            )}
          </Row>

          {resourceEntries.length > 0 && (
            <Row label="资源">
              {resourceEntries.map(([k, v]) => (
                <span key={k} className={TAG_CLASS}>
                  {k}: {String(v)}
                </span>
              ))}
            </Row>
          )}

          {variantGroups.length > 0 && (
            <div className="flex flex-col gap-1.5 border-t border-dashed border-border pt-1">
              {variantGroups.map((g) => (
                <div key={g.key} className="flex flex-col gap-1">
                  <span className="text-2xs font-semibold text-muted-foreground">
                    {g.title ?? g.key}
                  </span>
                  <div className="flex flex-wrap gap-1.5">
                    {g.variants!.map((v) => {
                      const metaBits = [
                        v.vram_gb != null ? `${v.vram_gb}GB` : null,
                        v.tier ? tierLabel(v.tier) : null,
                      ].filter(Boolean);
                      return (
                        <span
                          key={v.value}
                          className={
                            v.recommended
                              ? `${VARIANT_PILL_BASE} border-brand/30 bg-brand/10 text-brand`
                              : `${VARIANT_PILL_BASE} border-border bg-muted text-muted-foreground`
                          }
                          title={v.note ?? undefined}
                        >
                          <span className="mono">{v.label ?? v.value}</span>
                          {metaBits.length > 0 && (
                            <span className="text-muted-foreground"> · {metaBits.join(" · ")}</span>
                          )}
                          {v.recommended && <span className="text-status-caution"> ★</span>}
                        </span>
                      );
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}

          {evict && (
            <div className="flex items-center gap-1.5 border-t border-dashed border-border pt-2 text-xs text-muted-foreground">
              <Icon name="history" size={11} />
              <span>{formatEvict(evict)}</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-14 shrink-0 text-2xs font-semibold text-muted-foreground">{label}</span>
      <div className="flex flex-wrap gap-1.5">{children}</div>
    </div>
  );
}
