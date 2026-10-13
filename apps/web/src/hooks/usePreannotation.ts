import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { useReconnectingWebSocket, type ReconnectState } from "@/hooks/useReconnectingWebSocket";
import { useAuthStore } from "@/stores/authStore";
import { buildWsUrl } from "@/lib/wsHost";

/** v0.18.6 · 逐阶段实时统计项 (worker _stage_totals_snapshot 的形态)。源阶段=detected; 下游=targeted/ok/failed/skipped_geometry。 */
export interface PipelineStageStat {
  stage: number;
  detected?: number;
  targeted?: number;
  ok?: number;
  failed?: number;
  skipped_geometry?: number;
}

interface PreannotationProgress {
  current: number;
  total: number;
  status: "running" | "completed" | "error";
  error: string | null;
  /** v0.18.6 · 多阶段预标运行中的逐阶段累加快照 (worker 按 5% 步长推; 仅多阶段有)。 */
  pipeline_stages?: PipelineStageStat[];
}

export function usePreannotationProgress(projectId: string | undefined): {
  progress: PreannotationProgress | null;
  connection: ReconnectState;
  retries: number;
} {
  const [progress, setProgress] = useState<PreannotationProgress | null>(null);
  const token = useAuthStore((s) => s.token);

  // Root WS URL carries the current account token; wait for auth hydration and
  // reconnect on token/account change instead of connecting unauthenticated.
  const url =
    projectId && token ? buildWsUrl(`/ws/projects/${projectId}/preannotate`, { token }) : null;

  const onMessage = useCallback((e: MessageEvent) => {
    try {
      const msg = JSON.parse(e.data) as PreannotationProgress;
      // pipeline_stages 仅在 5% 步长帧带, 中间帧缺省 → 保留上一帧快照, 避免徽标闪空。
      setProgress((prev) => ({
        ...msg,
        pipeline_stages: msg.pipeline_stages ?? prev?.pipeline_stages,
      }));
    } catch {
      // ignore parse errors
    }
  }, []);

  const { state, retries } = useReconnectingWebSocket(url, { onMessage, enabled: !!url });

  return { progress, connection: state, retries };
}

export type TextOutputMode = "box" | "mask" | "both";

/** v0.11.24 · 预标幂等模式 */
export type PredictMode = "skip_predicted" | "overwrite" | "append";

export interface TriggerPreannotationPayload {
  ml_backend_id: string;
  task_ids?: string[];
  /** v0.9.5 · 文本批量预标可选项 */
  prompt?: string;
  output_mode?: TextOutputMode;
  batch_id?: string;
  /** v0.10.38 · 按后端参数面板 (epic 阶段 2): 选中 backend 的 /setup.params 值, 覆盖项目级阈值兜底. */
  params?: Record<string, unknown>;
  /** v0.11.24 · 跳过已预标 (默认) / 覆盖历史预标 / 追加 */
  predict_mode?: PredictMode;
  /** Issue #121 · 执行场景: "bulk" (缺省, 数据管理批量预标) | "workbench"
   *  (工作台「当前题 AI」单题交互)。工作台路径允许 in_progress 任务与 draft 批次,
   *  但仍保留管理员锁 / 他人编辑锁 / 终态任务保护。 */
  execution_scope?: "bulk" | "workbench";
  /** v0.14.9 · 能力声明协议 v2: 多模型 backend 时指定目标 model 条目 id. */
  model_id?: string;
  /** v0.14.9 · 任务类型便捷别名 ("ocr" / "doc_layout" / "text"); OCR / 版面预标透传. */
  task_type?: string;
  /** v0.14.17 · 协议 v2 结构化路径 (YOLO 等多 task 几何 backend): 选中 variant 组合 (dict[axis,value]).
   *  非空时后端构造 v2 context (model_variants dict + nested params), 修通 YOLO 批量预标. */
  model_variants?: Record<string, string>;
  /** v0.14.17 · 类别白名单 (模型原生类别 index 子集); 空/缺=全部类别. 仅几何 backend (YOLO) 用. */
  class_filter?: number[];
  /** v0.18.1 · 多阶段预标注 (路径 B): 有序阶段列表. 非空时走 detect→ROI→classify 编排;
   *  缺省=单阶段, 与现状逐字等价. 源阶段 (parent_stage=null) 的 ml_backend_id 须等于顶层. */
  pipeline_stages?: PipelineStagePayload[];
  /** v0.18.2 · 并行兄弟写同一属性键时的策略: reject (默认, 后端校验期 422) | last_wins (末位覆盖)。
   *  仅多阶段 (pipeline_stages 非空) 生效。 */
  on_key_conflict?: "reject" | "last_wins";
}

/** v0.21.5 · 初始输入节点数据源描述: 数据源 + 数据类型 + 执行单位。
 *  仅编排首节点(parent_stage=null)携带; 声明「源类型」维度(ROADMAP 方向 B/C)。
 *  execution_unit 决定下游分支: video=检测式追踪整段序列 / frame=逐帧(v0.21.7)。 */
export interface PipelineSource {
  kind: "dataset";
  /** 项目数据类型(image / video / lidar); 缺省按 image 兼容旧持久化。 */
  data_type?: string;
  /** 执行单位: 缺省 video(本版单分支); frame/scene 为后续。 */
  execution_unit?: "frame" | "video" | "scene";
}

/** v0.18.1 · 单个预标阶段声明; 字段对应后端 PipelineStage. */
export interface PipelineStagePayload {
  stage: number;
  ml_backend_id: string;
  /** v0.21.5 · 初始输入节点(parent_stage=null)的数据源描述; 下游 stage 无此字段。 */
  source?: PipelineSource;
  model_id?: string;
  task_type?: string;
  model_variants?: Record<string, string>;
  params?: Record<string, unknown>;
  class_filter?: number[];
  /** 依赖的父阶段 index; null/缺=源阶段 (吃整图). */
  parent_stage?: number | null;
  /** v0.18.2 · 父框类别白名单: 只对父框 class_name 命中时启动本阶段; 其余降级保留纯检测。 */
  parent_class_filter?: string[];
  /** ROI 构造: crop=裁父框喂纯分类; geometry=全图+父框列表喂 box-seg (v0.18.12)。 */
  roi?: { mode: "crop" | "geometry"; pad?: number };
  /** 结果写回: attributes=写父框 attributes; geometry=产独立 polygon shape (v0.18.12);
   *  intermediate=产几何仅供下游消费、不落库 (v0.18.14)。target_stage 本版仅 'root'。 */
  write?: {
    target: "attributes" | "geometry" | "intermediate";
    keys?: string[];
    target_stage?: "root";
  };
  /** v0.18.2 · 阶段级失败策略: keep_parent (默认, 上游框保留属性留空) | drop_box (丢父框)。 */
  on_failure?: "keep_parent" | "drop_box";
  /** v0.18.14 · 卡片显示名 + 写回属性键前缀 (子物体命名空间, 如 hat_color)。 */
  label?: string;
  /** v0.18.15 · 显式投递模式覆盖 (下游阶段只有 crop/geometry 两态); 缺省由后端按
   *  supported_inputs 烘焙。full_image 非真实投递模式, 后端校验会拒 (见 issue 0006)。 */
  input?: { mode: "crop" | "geometry" };
}

export interface TriggerPreannotationResponse {
  job_id: string;
  status: string;
  total_tasks?: number | null;
  channel?: string;
  /** v0.20.21 · 派发期软提示 (不拦截), 如 output=both + 多阶段会导致下游重复处理。 */
  warnings?: string[];
}

export function useTriggerPreannotation(projectId: string | undefined) {
  return useMutation({
    mutationFn: (payload: TriggerPreannotationPayload) => {
      if (!projectId) throw new Error("No project selected");
      return apiClient.post<TriggerPreannotationResponse>(
        `/projects/${projectId}/preannotate`,
        payload,
      );
    },
  });
}
