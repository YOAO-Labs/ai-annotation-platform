import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { apiClient } from "@/api/client";
import { useReconnectingWebSocket, type ReconnectState } from "@/hooks/useReconnectingWebSocket";
import { useAuthStore } from "@/stores/authStore";
import { buildWsUrl } from "@/lib/wsHost";
import { randomId } from "@/utils/id";

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
  pipeline_stages?: PipelineStageStat[];
}

export function usePreannotationProgress(projectId: string | undefined): {
  progress: PreannotationProgress | null;
  connection: ReconnectState;
  retries: number;
} {
  const [progress, setProgress] = useState<PreannotationProgress | null>(null);
  const token = useAuthStore((s) => s.token);

  const url =
    projectId && token ? buildWsUrl(`/ws/projects/${projectId}/preannotate`, { token }) : null;

  const onMessage = useCallback((e: MessageEvent) => {
    try {
      const msg = JSON.parse(e.data) as PreannotationProgress;
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

export type PredictMode = "skip_predicted" | "overwrite" | "append";

export interface TriggerPreannotationPayload {
  ml_backend_id: string;
  task_ids?: string[];
  prompt?: string;
  output_mode?: TextOutputMode;
  batch_id?: string;
  params?: Record<string, unknown>;
  predict_mode?: PredictMode;
  execution_scope?: "bulk" | "workbench";
  model_id?: string;
  task?: string;
  model_variants?: Record<string, string>;
  class_filter?: number[];
  pipeline_stages?: PipelineStagePayload[];
  on_key_conflict?: "reject" | "last_wins";
}

export interface PipelineSource {
  kind: "dataset";
  data_type?: string;
  execution_unit?: "frame" | "video" | "scene";
}

export interface PipelineStagePayload {
  stage: number;
  ml_backend_id: string;
  source?: PipelineSource;
  model_id?: string;
  task?: string;
  model_variants?: Record<string, string>;
  params?: Record<string, unknown>;
  class_filter?: number[];
  parent_stage?: number | null;
  parent_class_filter?: string[];
  roi?: { mode: "crop" | "geometry"; pad?: number };
  write?: {
    target: "attributes" | "geometry" | "intermediate";
    keys?: string[];
    target_stage?: "root";
  };
  on_failure?: "keep_parent" | "drop_box";
  label?: string;
  input?: { mode: "crop" | "geometry" };
}

export interface TriggerPreannotationResponse {
  job_id: string;
  status: string;
  total_tasks?: number | null;
  channel?: string;
  warnings?: string[];
}

type TriggerPreannotationVariables =
  | TriggerPreannotationPayload
  | { payload: TriggerPreannotationPayload; idempotencyKey: string };

export function createPreannotationIdempotencyKey(): string {
  return randomId();
}

export function useTriggerPreannotation(projectId: string | undefined) {
  return useMutation({
    mutationFn: (variables: TriggerPreannotationVariables) => {
      if (!projectId) throw new Error("No project selected");
      const wrapped = "payload" in variables;
      const payload = wrapped ? variables.payload : variables;
      const idempotencyKey = wrapped ? variables.idempotencyKey : undefined;
      return apiClient.post<TriggerPreannotationResponse>(
        `/projects/${projectId}/preannotate`,
        payload,
        idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
      );
    },
  });
}
