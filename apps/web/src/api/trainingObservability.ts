import { apiClient } from "./client";
import type { LogPage, TelemetryPage, TrainingRecovery } from "./generated/types.gen";

export type {
  LogPage,
  TelemetryPage,
  TrainingRecovery,
  TelemetryOut,
  TrainingFailure,
} from "./generated/types.gen";

const base = (project: string, run: string) => `/projects/${project}/training-runs/${run}`;
export const trainingObservabilityApi = {
  telemetry: (
    project: string,
    run: string,
    attempt: string,
    after?: number,
    options?: RequestInit,
  ) =>
    apiClient.get<TelemetryPage>(
      `${base(project, run)}/attempts/${attempt}/telemetry${after == null ? "" : `?after=${after}`}`,
      options,
    ),
  logs: (
    project: string,
    run: string,
    attempt: string,
    cursor: { after?: number; before?: number } = {},
    options?: RequestInit,
  ) => {
    const params = new URLSearchParams();
    if (cursor.after != null) params.set("after", String(cursor.after));
    if (cursor.before != null) params.set("before", String(cursor.before));
    return apiClient.get<LogPage>(
      `${base(project, run)}/attempts/${attempt}/logs${params.size ? `?${params}` : ""}`,
      options,
    );
  },
  recovery: (project: string, run: string, options?: RequestInit) =>
    apiClient.get<TrainingRecovery>(`${base(project, run)}/recovery`, options),
};
