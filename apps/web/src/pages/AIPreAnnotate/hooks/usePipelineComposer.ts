/**
 * 编排搭建的共用状态机 hook (v0.21.0 收尾优化 · 方案 B refactor).
 *
 * 把 ProjectDetailPanel 与 GlobalPipelineLibraryPage 都要的 "受限 DAG + 键冲突判据"
 * 从两页里提出来. 只共用**结构层** — 阶段图 (stagesGraph) / 各卡 payload+caps 收集 /
 * 加子/删/改父 / 键冲突检测. 不共用"运行态展示" (stageStats/sourceDetected 等) 与
 * "Inspector 参数编辑" (项目侧 StageCard 深依赖 projectId, 全局侧走精简 GlobalStageInspector).
 *
 * Context 参数仅 availableBackendCount — 源恒产几何, 项目侧从 backends.length 传入, 全局侧
 * 从全局池里 unique backend 数派生.
 */
import { useCallback, useMemo, useRef, useState } from "react";

import type { PipelineStagePayload } from "@/hooks/usePreannotation";
import {
  MAX_DEPTH,
  ROOT_SID,
  SOURCE_SID,
  canAddChild as pureCanAddChild,
  canReparent,
  depthBySid,
  descendantsOf,
  reparent,
  type StageCaps,
  type StageEntry,
} from "../utils/pipelineGraph";

// v0.21.6 · 终态初始图: 输入节点(纯数据源) + 一个首模型 stage(承接源模型配置)。
const initialGraph = (): StageEntry[] => [
  { sid: ROOT_SID, parentSid: null },
  { sid: SOURCE_SID, parentSid: ROOT_SID },
];

export interface UsePipelineComposerArgs {
  /**
   * 可用 backend 数. 项目侧 = 项目已启用 backend 数; 全局侧 = 全局池 unique backend 数.
   * <2 时不允许加子 (下游须用不同于源检测的 backend).
   */
  availableBackendCount: number;
  hasCompatibleDownstream?: boolean;
  /**
   * 加子超深/其他 UI 兜底提示 (可选). 未传时静默拒绝.
   */
  onWarn?: (msg: string, sub?: string) => void;
  /**
   * 级联删后代时的提示 (可选).
   */
  onCascadeDelete?: (killedCount: number) => void;
}

export interface UsePipelineComposerReturn {
  stagesGraph: StageEntry[];
  setStagesGraph: React.Dispatch<React.SetStateAction<StageEntry[]>>;
  selectedSid: string;
  setSelectedSid: React.Dispatch<React.SetStateAction<string>>;

  onStageChange: (sid: string, payload: PipelineStagePayload | null) => void;
  onStageCaps: (sid: string, caps: StageCaps | null) => void;
  stageCapsRef: React.MutableRefObject<Record<string, StageCaps | null>>;

  downstreamPayloads: (PipelineStagePayload | null)[];
  allDownstreamReady: boolean;
  payloadBySid: Record<string, PipelineStagePayload | null>;

  addStage: (parentSid: string) => void;
  removeStage: (sid: string) => void;
  onReparent: (childSid: string, newParentSid: string) => void;
  canReparentConn: (childSid: string, newParentSid: string) => boolean;
  canAddChildAt: (parentSid: string) => boolean;

  /**
   * 属性键冲突检测 (多个 attributes 阶段写同一「最终键」= 前缀+key).
   * displayFinals 用于顶部提示; perCard[sid] 用于 chip 标红.
   */
  conflictInfo: {
    conflictFinals: Set<string>;
    perCard: Record<string, Set<string>>;
    displayFinals: Set<string>;
  };
  hasKeyConflict: boolean;

  /** projectId / 编排入口切换时重置 (调用方在 useEffect 里触发). */
  reset: () => void;
}

/**
 * 编排搭建共用 hook. 单元测: `usePipelineComposer.test.ts` (承接项目侧 pipelineGraph 判据覆盖).
 */
export function usePipelineComposer(args: UsePipelineComposerArgs): UsePipelineComposerReturn {
  const { availableBackendCount, onWarn, onCascadeDelete } = args;

  // v0.21.6 · 输入节点(纯数据源) + 首模型 stage 常驻; 默认选中首模型 stage(输入节点是被动数据源)。
  const [stagesGraph, setStagesGraph] = useState<StageEntry[]>(initialGraph);
  const [selectedSid, setSelectedSid] = useState<string>(SOURCE_SID);
  const stagePayloadsRef = useRef<Record<string, PipelineStagePayload | null>>({});
  const stageCapsRef = useRef<Record<string, StageCaps | null>>({});
  const [stageTick, setStageTick] = useState(0);
  const seqRef = useRef(0);

  const onStageChange = useCallback((sid: string, payload: PipelineStagePayload | null) => {
    stagePayloadsRef.current[sid] = payload;
    setStageTick((n) => n + 1);
  }, []);
  const onStageCaps = useCallback((sid: string, caps: StageCaps | null) => {
    stageCapsRef.current[sid] = caps;
    setStageTick((n) => n + 1);
  }, []);

  const canAddBackend = args.hasCompatibleDownstream ?? availableBackendCount >= 2;

  const addStage = useCallback(
    (parentSid: string) => {
      // 输入节点 depth=1, 通用判据天然放行; 不再 ROOT_SID 特判。
      if ((depthBySid(stagesGraph)[parentSid] ?? 1) >= MAX_DEPTH) {
        onWarn?.("无法加子阶段", `流水线最深 ${MAX_DEPTH} 层`);
        return;
      }
      const sid = `stage-${(seqRef.current += 1)}`;
      setStagesGraph((g) => [...g, { sid, parentSid }]);
      setSelectedSid(sid);
    },
    [stagesGraph, onWarn],
  );

  const removeStage = useCallback(
    (sid: string) => {
      // 输入节点(纯数据源, parentSid=null)与首模型 stage(SOURCE_SID, 每流水线必有一个模型)不可删。
      const target = stagesGraph.find((e) => e.sid === sid);
      if (!target || target.parentSid == null || sid === SOURCE_SID) return;
      const kids = descendantsOf(stagesGraph, sid);
      if (kids.size > 0) onCascadeDelete?.(kids.size);
      setStagesGraph((g) => {
        const dead = new Set([sid]);
        for (let changed = true; changed; ) {
          changed = false;
          for (const e of g) {
            if (e.parentSid != null && dead.has(e.parentSid) && !dead.has(e.sid)) {
              dead.add(e.sid);
              changed = true;
            }
          }
        }
        setSelectedSid((cur) => (dead.has(cur) ? SOURCE_SID : cur));
        return g.filter((e) => !dead.has(e.sid));
      });
    },
    [stagesGraph, onCascadeDelete],
  );

  const downstreamPayloads = useMemo(
    () => stagesGraph.map((e) => stagePayloadsRef.current[e.sid] ?? null),
    // stagePayloadsRef 是 ref, 卡片回报后靠 stageTick 触发重算.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [stagesGraph, stageTick],
  );
  // v0.21.6 · 输入节点(纯数据源)与源式 stage(父=输入节点, 走 cfg/PreannotateConfigForm) 的就绪
  // 由调用方 cfg.configReady 门控, 不进复合器 —— 这里仅判 StageCard 配置的下游卡(父=模型 stage)。
  const allDownstreamReady = stagesGraph.every(
    (e, i) => e.parentSid == null || e.parentSid === ROOT_SID || downstreamPayloads[i] != null,
  );

  const payloadBySid = useMemo(() => {
    const m: Record<string, PipelineStagePayload | null> = {};
    stagesGraph.forEach((e, i) => {
      m[e.sid] = downstreamPayloads[i] ?? null;
    });
    return m;
  }, [stagesGraph, downstreamPayloads]);

  const canReparentConn = useCallback(
    (childSid: string, newParentSid: string) =>
      canReparent(stagesGraph, payloadBySid, childSid, newParentSid).ok,
    [stagesGraph, payloadBySid],
  );
  const onReparent = useCallback(
    (childSid: string, newParentSid: string) => {
      const chk = canReparent(stagesGraph, payloadBySid, childSid, newParentSid);
      if (!chk.ok) {
        if (chk.reason) onWarn?.("无法改父", chk.reason);
        return;
      }
      setStagesGraph((g) => reparent(g, childSid, newParentSid));
      setSelectedSid(childSid);
    },
    [stagesGraph, payloadBySid, onWarn],
  );

  const canAddChildAt = useCallback(
    (parentSid: string) => canAddBackend && pureCanAddChild(stagesGraph, payloadBySid, parentSid),
    [canAddBackend, stagesGraph, payloadBySid],
  );

  const conflictInfo = useMemo(() => {
    const counts = new Map<string, number>();
    downstreamPayloads.forEach((p) => {
      if (p?.write?.target !== "attributes") return;
      const prefix = p.label ? `${p.label}_` : "";
      for (const k of p.write.keys ?? []) counts.set(prefix + k, (counts.get(prefix + k) ?? 0) + 1);
    });
    const conflictFinals = new Set(
      Array.from(counts)
        .filter(([, n]) => n >= 2)
        .map(([k]) => k),
    );
    const perCard: Record<string, Set<string>> = {};
    const displayFinals = new Set<string>();
    stagesGraph.forEach((e, i) => {
      const p = downstreamPayloads[i];
      if (p?.write?.target !== "attributes") return;
      const prefix = p.label ? `${p.label}_` : "";
      const set = new Set<string>();
      for (const k of p.write.keys ?? []) {
        if (conflictFinals.has(prefix + k)) {
          set.add(k);
          displayFinals.add(prefix + k);
        }
      }
      if (set.size) perCard[e.sid] = set;
    });
    return { conflictFinals, perCard, displayFinals };
  }, [stagesGraph, downstreamPayloads]);
  const hasKeyConflict = conflictInfo.conflictFinals.size > 0;

  const reset = useCallback(() => {
    setStagesGraph(initialGraph());
    setSelectedSid(SOURCE_SID);
    stagePayloadsRef.current = {};
    stageCapsRef.current = {};
    seqRef.current = 0;
  }, []);

  return {
    stagesGraph,
    setStagesGraph,
    selectedSid,
    setSelectedSid,
    onStageChange,
    onStageCaps,
    stageCapsRef,
    downstreamPayloads,
    allDownstreamReady,
    payloadBySid,
    addStage,
    removeStage,
    onReparent,
    canReparentConn,
    canAddChildAt,
    conflictInfo,
    hasKeyConflict,
    reset,
  };
}
