import { useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { KeypointNode, KeypointSchema } from "@/types";
import { keypointColorByIndex } from "@/pages/Workbench/stage/ImageStageShapes";

const ICON_BTN_CLASS =
  "inline-flex size-7 shrink-0 cursor-pointer appearance-none items-center justify-center rounded border border-border bg-transparent p-0 text-muted-foreground enabled:hover:bg-card enabled:hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40";
const FIELD_CLASS =
  "h-7 min-w-0 flex-1 appearance-none rounded border border-border bg-card px-2 text-sm text-foreground outline-none";

const EMPTY: KeypointSchema = { nodes: [], edges: [] };

const DRAG_THRESHOLD = 0.02;

const clamp01 = (v: number) => (v < 0 ? 0 : v > 1 ? 1 : v);

export function keypointNodePos(
  node: KeypointNode,
  i: number,
  total: number,
): { x: number; y: number } {
  if (node.x != null && node.y != null) return { x: node.x, y: node.y };
  const angle = (i / Math.max(total, 1)) * Math.PI * 2 - Math.PI / 2;
  return { x: 0.5 + 0.36 * Math.cos(angle), y: 0.5 + 0.36 * Math.sin(angle) };
}

export function KeypointSchemaEditor({
  value,
  onChange,
}: {
  value: KeypointSchema | null | undefined;
  onChange: (next: KeypointSchema) => void;
}) {
  const schema = value ?? EMPTY;
  const { nodes, edges } = schema;

  const svgRef = useRef<SVGSVGElement>(null);
  const [linkFrom, setLinkFrom] = useState<number | null>(null);
  const dragRef = useRef<{ idx: number; moved: boolean; startX: number; startY: number } | null>(
    null,
  );

  const setNodes = (next: KeypointSchema["nodes"]) => {
    onChange({ nodes: next, edges });
  };

  const patchNode = (idx: number, patch: Partial<KeypointNode>) => {
    setNodes(nodes.map((n, i) => (i === idx ? { ...n, ...patch } : n)));
  };

  const addNode = () => {
    const i = nodes.length;
    const pos = keypointNodePos({ name: "" }, i, i + 1);
    onChange({
      nodes: [
        ...nodes,
        { name: `node_${i + 1}`, color: keypointColorByIndex(i), x: pos.x, y: pos.y },
      ],
      edges,
    });
  };

  const removeNode = (idx: number) => {
    const nextNodes = nodes.filter((_, i) => i !== idx);
    const nextEdges = edges
      .filter(([i, j]) => i !== idx && j !== idx)
      .map(([i, j]) => [i > idx ? i - 1 : i, j > idx ? j - 1 : j] as [number, number]);
    onChange({ nodes: nextNodes, edges: nextEdges });
    if (linkFrom === idx) setLinkFrom(null);
  };

  const moveNode = (idx: number, dir: -1 | 1) => {
    const j = idx + dir;
    if (j < 0 || j >= nodes.length) return;
    const nextNodes = nodes.slice();
    [nextNodes[idx], nextNodes[j]] = [nextNodes[j], nextNodes[idx]];
    const swap = (k: number) => (k === idx ? j : k === j ? idx : k);
    const nextEdges = edges.map(([i, k]) => [swap(i), swap(k)] as [number, number]);
    onChange({ nodes: nextNodes, edges: nextEdges });
  };

  const addEdge = (a: number, b: number) => {
    if (a === b || a >= nodes.length || b >= nodes.length) return;
    const exists = edges.some(([i, j]) => (i === a && j === b) || (i === b && j === a));
    if (exists) return;
    onChange({ nodes, edges: [...edges, [a, b]] });
  };

  const removeEdge = (idx: number) => {
    onChange({ nodes, edges: edges.filter((_, i) => i !== idx) });
  };

  const toNorm = (clientX: number, clientY: number) => {
    const svg = svgRef.current;
    if (!svg) return null;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;
    return {
      x: clamp01((clientX - rect.left) / rect.width),
      y: clamp01((clientY - rect.top) / rect.height),
    };
  };

  const onNodePointerDown = (i: number, e: React.PointerEvent) => {
    e.preventDefault();
    (e.currentTarget as Element).setPointerCapture?.(e.pointerId);
    const p = toNorm(e.clientX, e.clientY);
    dragRef.current = { idx: i, moved: false, startX: p?.x ?? 0, startY: p?.y ?? 0 };
  };

  const onSvgPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const p = toNorm(e.clientX, e.clientY);
    if (!p) return;
    if (!d.moved && Math.hypot(p.x - d.startX, p.y - d.startY) > DRAG_THRESHOLD) d.moved = true;
    if (d.moved) patchNode(d.idx, { x: p.x, y: p.y });
  };

  const onNodePointerUp = (i: number, e: React.PointerEvent) => {
    const d = dragRef.current;
    dragRef.current = null;
    (e.currentTarget as Element).releasePointerCapture?.(e.pointerId);
    if (!d || d.moved) return;
    if (linkFrom == null) setLinkFrom(i);
    else if (linkFrom !== i) {
      addEdge(linkFrom, i);
      setLinkFrom(null);
    } else setLinkFrom(null);
  };

  return (
    <div className="mt-3 grid grid-cols-1 gap-5 md:grid-cols-2">
      <div className="flex flex-col gap-2">
        <span className="text-sm font-semibold text-foreground">骨骼画布</span>
        <svg
          ref={svgRef}
          viewBox="0 0 100 100"
          role="img"
          aria-label="骨骼模板画布"
          data-testid="keypoint-canvas"
          className="aspect-square w-full touch-none rounded-md border border-border bg-muted text-muted-foreground"
          onPointerMove={onSvgPointerMove}
          onPointerUp={() => {
            dragRef.current = null;
          }}
          onClick={(e) => {
            if (e.target === svgRef.current) setLinkFrom(null);
          }}
        >
          {edges.map(([i, j], idx) => {
            const a = nodes[i];
            const b = nodes[j];
            if (!a || !b) return null;
            const pa = keypointNodePos(a, i, nodes.length);
            const pb = keypointNodePos(b, j, nodes.length);
            return (
              <line
                key={`edge-${idx}`}
                x1={pa.x * 100}
                y1={pa.y * 100}
                x2={pb.x * 100}
                y2={pb.y * 100}
                stroke="currentColor"
                strokeWidth={1}
                strokeLinecap="round"
                className="cursor-pointer hover:opacity-60"
                onClick={(e) => {
                  e.stopPropagation();
                  removeEdge(idx);
                }}
              >
                <title>点击删除连线</title>
              </line>
            );
          })}
          {nodes.map((n, i) => {
            const p = keypointNodePos(n, i, nodes.length);
            const isLink = linkFrom === i;
            return (
              <g key={`node-${i}`}>
                <circle
                  cx={p.x * 100}
                  cy={p.y * 100}
                  r={isLink ? 3.6 : 2.8}
                  fill={n.color ?? keypointColorByIndex(i)}
                  stroke={isLink ? "currentColor" : "white"}
                  strokeWidth={isLink ? 1.2 : 0.7}
                  className="cursor-grab"
                  data-testid={`keypoint-node-${i}`}
                  onPointerDown={(e) => onNodePointerDown(i, e)}
                  onPointerUp={(e) => onNodePointerUp(i, e)}
                >
                  <title>{n.sublabel ? `${n.name} · ${n.sublabel}` : n.name}</title>
                </circle>
                <text
                  x={p.x * 100 + 4}
                  y={p.y * 100 + 1.2}
                  fontSize={3.4}
                  fill="currentColor"
                  className="pointer-events-none select-none"
                >
                  {i + 1}
                </text>
              </g>
            );
          })}
        </svg>
        <p className="m-0 text-xs text-muted-foreground">
          {nodes.length === 0
            ? "右侧「新增节点」后，可在此拖动节点定义骨架布局。"
            : linkFrom != null
              ? `已选节点 ${linkFrom + 1}，点击另一节点连线（点击空白取消）。`
              : "拖动节点调整布局 · 点击两个节点连线 · 点击连线删除。"}
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold text-foreground">关键点节点 ({nodes.length})</span>
          <Button variant="ghost" size="sm" onClick={addNode}>
            <Icon name="plus" size={14} /> 新增节点
          </Button>
        </div>
        {nodes.length === 0 ? (
          <p className="m-0 text-xs text-muted-foreground">
            尚无节点。点击「新增节点」开始定义骨骼。
          </p>
        ) : (
          <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
            {nodes.map((n, i) => (
              <li
                key={i}
                className="flex items-center gap-2 rounded-md border border-border bg-muted px-2 py-1.5"
              >
                <span className="w-5 shrink-0 text-center text-xs text-muted-foreground">
                  {i + 1}
                </span>
                <input
                  type="color"
                  value={n.color ?? keypointColorByIndex(i)}
                  onChange={(e) => patchNode(i, { color: e.target.value })}
                  className="h-6 w-7 shrink-0 cursor-pointer rounded border border-border bg-card p-0"
                  aria-label={`节点 ${i + 1} 颜色`}
                />
                <input
                  type="text"
                  value={n.name}
                  onChange={(e) => patchNode(i, { name: e.target.value })}
                  className={FIELD_CLASS}
                  placeholder="节点名"
                  aria-label={`节点 ${i + 1} 名称`}
                />
                <input
                  type="text"
                  value={n.sublabel ?? ""}
                  onChange={(e) => patchNode(i, { sublabel: e.target.value || null })}
                  className={`${FIELD_CLASS} max-w-24`}
                  placeholder="子标签"
                  aria-label={`节点 ${i + 1} 子标签`}
                />
                <button
                  type="button"
                  className={ICON_BTN_CLASS}
                  onClick={() => moveNode(i, -1)}
                  disabled={i === 0}
                  title="上移"
                >
                  <Icon name="chevUp" size={14} />
                </button>
                <button
                  type="button"
                  className={ICON_BTN_CLASS}
                  onClick={() => moveNode(i, 1)}
                  disabled={i === nodes.length - 1}
                  title="下移"
                >
                  <Icon name="chevDown" size={14} />
                </button>
                <button
                  type="button"
                  className={ICON_BTN_CLASS}
                  onClick={() => removeNode(i)}
                  title="删除节点"
                >
                  <Icon name="trash" size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}
        {edges.length > 0 && (
          <p className="m-0 text-xs text-muted-foreground">
            骨骼连线 {edges.length} 条（在画布点击连线可删除）。
          </p>
        )}
      </div>
    </div>
  );
}
