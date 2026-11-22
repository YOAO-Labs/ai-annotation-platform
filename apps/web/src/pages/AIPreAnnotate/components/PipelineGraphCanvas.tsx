import {
  createContext,
  memo,
  useContext,
  useEffect,
  useMemo,
  useCallback,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  useReactFlow,
  useNodesInitialized,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
  type IsValidConnection,
  type OnConnectEnd,
  type OnConnectStart,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { ProgressBar } from "@/components/ui/ProgressBar";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  ROOT_SID,
  buildFlow,
  type GraphNodeModel,
  type StageNodeData,
} from "../utils/pipelineGraph";
import styles from "./PipelineGraphCanvas.module.css";

interface CanvasCallbacks {
  onSelect: (sid: string) => void;
  onAddChild: (parentSid: string) => void;
  onRemove: (sid: string) => void;
  connectingFrom: string | null;
  canReparentConn: (childSid: string, newParentSid: string) => boolean;
}
const CanvasCtx = createContext<CanvasCallbacks>({
  onSelect: () => {},
  onAddChild: () => {},
  onRemove: () => {},
  connectingFrom: null,
  canReparentConn: () => false,
});

function cx(...names: Array<string | false | null | undefined>) {
  return names.filter(Boolean).join(" ");
}

const DOT_CLASS: Record<string, string> = {
  pending: styles.dotPending,
  running: styles.dotRunning,
  done: styles.dotDone,
};

function tooltipDesc(data: StageNodeData, isSource: boolean) {
  return (
    <div className={styles.tipBody}>
      {data.backendName && <div>模型服务：{data.backendName}</div>}
      {data.modelId && <div>模型：{data.modelId}</div>}
      {data.taskType && <div>任务：{data.taskType}</div>}
      {!isSource && <div>父框：{data.classFilter ?? "全部框"}</div>}
      {data.roiInfo && <div>投递：{data.roiInfo}</div>}
      {data.variantInfo && <div>变体：{data.variantInfo}</div>}
      <div>产物：{data.detail}</div>
      {data.warning && <div className={styles.tipWarn}>⚠ {data.warning}</div>}
    </div>
  );
}

function NodeBody({ data }: { data: StageNodeData }) {
  const { onSelect, onAddChild, onRemove, connectingFrom, canReparentConn } = useContext(CanvasCtx);
  const isSource = data.parentSid == null;

  const drop =
    connectingFrom && connectingFrom !== data.sid
      ? canReparentConn(data.sid, connectingFrom)
        ? "ok"
        : "bad"
      : null;
  const okPct = data.targeted && data.targeted > 0 ? ((data.ok ?? 0) / data.targeted) * 100 : 0;

  return (
    <Tooltip
      name={isSource ? `${data.role.label}（输入）` : data.role.label}
      desc={tooltipDesc(data, isSource)}
      side="top"
    >
      <div
        className={cx(
          styles.node,
          data.selected && styles.nodeSelected,
          data.conflict && styles.nodeConflict,
          !data.ready && styles.nodeUnconfigured,
          drop === "ok" && styles.nodeDropOk,
          drop === "bad" && styles.nodeDropBad,
        )}
        onClick={() => onSelect(data.sid)}
      >
        <div className={styles.nodeHeader}>
          <Icon name={data.role.icon} size={12} className={styles.nodeIcon} />
          <Badge variant={isSource ? "outline" : data.role.variant}>
            {isSource ? "输入" : data.role.label}
          </Badge>
          <span className={styles.nodeName}>{isSource ? data.role.label : ""}</span>
          {isSource && data.sourceTypeLabel && (
            <Badge variant="outline">{data.sourceTypeLabel}</Badge>
          )}
          {isSource && data.executionUnitLabel && (
            <Badge variant="outline">{data.executionUnitLabel}</Badge>
          )}
          {data.warning && <Icon name="warning" size={12} className={styles.nodeWarn} />}
          <span className={cx(styles.dot, DOT_CLASS[data.runState])} title={data.runState} />
        </div>

        <div className={styles.nodeSubtitle}>
          <span className={styles.nodeBackend}>{data.backendName ?? "—"}</span>
          {!data.ready && <Badge variant="outline">待配置</Badge>}
        </div>

        <span className={styles.nodeDetail} title={data.detail}>
          {data.detail}
        </span>

        {!isSource && data.classFilter && (
          <span className={styles.nodeChip}>{data.classFilter}</span>
        )}

        {!isSource && data.runState === "running" && data.targeted ? (
          <ProgressBar value={okPct} />
        ) : null}

        <div className={styles.nodeFooter}>
          <span className={styles.nodeCounts}>
            {isSource
              ? data.ok != null && (
                  <span>
                    {data.sourceCountLabel ?? "检出"} {data.ok}
                  </span>
                )
              : data.targeted != null && (
                  <>
                    <span>目标 {data.targeted}</span>
                    <span>成功 {data.ok ?? 0}</span>
                  </>
                )}
          </span>
          <span className={styles.nodeActions}>
            {data.canAddChild && (
              <button
                type="button"
                className={styles.nodeBtn}
                title="加子阶段（对该阶段产出的每个框继续跑）"
                onClick={(e) => {
                  e.stopPropagation();
                  onAddChild(data.sid);
                }}
              >
                <Icon name="plus" size={12} />
              </button>
            )}
            {!isSource && (
              <button
                type="button"
                className={styles.nodeBtn}
                title="移除该阶段"
                onClick={(e) => {
                  e.stopPropagation();
                  onRemove(data.sid);
                }}
              >
                <Icon name="trash" size={12} />
              </button>
            )}
          </span>
        </div>
      </div>
    </Tooltip>
  );
}

const StageNode = memo(({ data }: NodeProps<Node<StageNodeData>>) => (
  <>
    {data.parentSid != null && <Handle type="target" position={Position.Left} />}
    <NodeBody data={data} />
    {(data.canAddChild || data.parentSid == null) && (
      <Handle type="source" position={Position.Right} />
    )}
  </>
));
StageNode.displayName = "StageNode";

const nodeTypes = { stage: StageNode };

interface Props {
  models: GraphNodeModel[];
  selectedSid: string | null;
  onSelect: (sid: string) => void;
  onAddChild: (parentSid: string) => void;
  onRemove: (sid: string) => void;
  onReparent: (childSid: string, newParentSid: string) => void;
  canReparentConn: (childSid: string, newParentSid: string) => boolean;
}

function Flow({
  models,
  selectedSid,
  onSelect,
  onAddChild,
  onRemove,
  onReparent,
  canReparentConn,
}: Props) {
  const flow = useMemo(() => buildFlow(models, selectedSid), [models, selectedSid]);
  const [nodes, setNodes, onNodesChange] = useNodesState(flow.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(flow.edges);
  const topoFingerprint = useMemo(
    () =>
      flow.nodes
        .map((n) => `${n.id}|${(n.data as { parentSid?: string }).parentSid ?? ""}`)
        .sort()
        .join(","),
    [flow.nodes],
  );
  useEffect(() => {
    setNodes((prev) => {
      const measuredById = new Map(prev.map((n) => [n.id, n.measured]));
      return flow.nodes.map((n) => {
        const measured = measuredById.get(n.id);
        return measured ? { ...n, measured } : n;
      });
    });
  }, [flow.nodes, setNodes]);
  const lastTopoRef = useRef<string>(topoFingerprint);
  useEffect(() => {
    if (lastTopoRef.current !== topoFingerprint) {
      lastTopoRef.current = topoFingerprint;
      setEdges(flow.edges);
    }
  }, [topoFingerprint, flow.edges, setEdges]);

  const rf = useReactFlow();
  const nodesInitialized = useNodesInitialized();
  useEffect(() => {
    if (nodesInitialized) rf.fitView({ padding: 0.2, maxZoom: 1 });
  }, [nodesInitialized, topoFingerprint, rf]);

  const [connectingFrom, setConnectingFrom] = useState<string | null>(null);
  const onConnectStart = useCallback<OnConnectStart>((_e, params) => {
    if (params.handleType === "source") setConnectingFrom(params.nodeId ?? null);
  }, []);

  const onConnect = useCallback(
    (c: Connection) => {
      if (c.source && c.target) onReparent(c.target, c.source);
    },
    [onReparent],
  );
  const onReconnect = useCallback(
    (_old: Edge, c: Connection) => {
      if (c.source && c.target) onReparent(c.target, c.source);
    },
    [onReparent],
  );
  const isValidConnection = useCallback<IsValidConnection>(
    (c) => !!c.source && !!c.target && canReparentConn(c.target, c.source),
    [canReparentConn],
  );
  const onConnectEnd = useCallback<OnConnectEnd>(
    (_event, conn) => {
      if (conn.fromNode && !conn.toNode) onAddChild(conn.fromNode.id);
      setConnectingFrom(null);
    },
    [onAddChild],
  );
  const onNodesDelete = useCallback(
    (deleted: Node[]) => {
      deleted.forEach((n) => {
        if (n.id !== ROOT_SID) onRemove(n.id);
      });
    },
    [onRemove],
  );

  const onKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (!selectedSid) return;
      const cur = models.find((m) => m.sid === selectedSid);
      if (!cur) return;
      let next: string | undefined;
      if (e.key === "ArrowLeft") next = cur.parentSid ?? undefined;
      else if (e.key === "ArrowRight") next = models.find((m) => m.parentSid === selectedSid)?.sid;
      else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        const sibs = models.filter((m) => m.parentSid === cur.parentSid);
        const idx = sibs.findIndex((m) => m.sid === selectedSid);
        next = sibs[idx + (e.key === "ArrowUp" ? -1 : 1)]?.sid;
      } else return;
      if (next) {
        e.preventDefault();
        onSelect(next);
      }
    },
    [models, selectedSid, onSelect],
  );

  const callbacks = useMemo<CanvasCallbacks>(
    () => ({ onSelect, onAddChild, onRemove, connectingFrom, canReparentConn }),
    [onSelect, onAddChild, onRemove, connectingFrom, canReparentConn],
  );

  return (
    <div className={styles.canvas} tabIndex={0} onKeyDown={onKeyDown}>
      <CanvasCtx.Provider value={callbacks}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          nodeTypes={nodeTypes}
          nodesDraggable={false}
          nodesConnectable
          elementsSelectable
          fitView
          fitViewOptions={{ padding: 0.2, maxZoom: 1 }}
          proOptions={{ hideAttribution: true }}
          onConnectStart={onConnectStart}
          onConnect={onConnect}
          onReconnect={onReconnect}
          onConnectEnd={onConnectEnd}
          onNodesDelete={onNodesDelete}
          isValidConnection={isValidConnection}
          onNodeClick={(_e, n) => onSelect(n.id)}
        >
          <Background gap={16} />
          <Controls showInteractive={false} />
          {flow.nodes.length > 4 && <MiniMap pannable zoomable />}
        </ReactFlow>
      </CanvasCtx.Provider>
    </div>
  );
}

export default function PipelineGraphCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <Flow {...props} />
    </ReactFlowProvider>
  );
}
