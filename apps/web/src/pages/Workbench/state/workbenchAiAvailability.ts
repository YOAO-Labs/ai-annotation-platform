import type { CapIndex } from "./useBackendRouting";

export function resolveWorkbenchAiRunUnavailableReason({
  backendId,
  backends,
  capIndex,
  unavailableReason,
}: {
  backendId: string | null;
  backends: readonly { id: string; state: string }[];
  capIndex: CapIndex;
  unavailableReason?: string;
}): string | undefined {
  const backend = backends.find((item) => item.id === backendId);
  if (backend?.state === "connected" && capIndex[backend.id]?.reachable) return undefined;
  return unavailableReason ?? "当前 AI 后端不可用，请在推理面板中选择已连接的后端";
}

/** Entry admission is project-wide; model/prompt configuration belongs inside the panel. */
export function resolveWorkbenchAiAvailability({
  backends,
  capIndex,
  backendsLoading,
  backendsError,
  capabilitiesLoading,
  hasTrackers,
}: {
  backends: readonly { id: string; state: string }[];
  capIndex: CapIndex;
  backendsLoading: boolean;
  backendsError: boolean;
  capabilitiesLoading: boolean;
  hasTrackers: boolean;
}): { aiUnavailableReason?: string; trackerUnavailableReason?: string } {
  let reason: string | undefined;
  if (
    backends.some((backend) => backend.state === "connected" && capIndex[backend.id]?.reachable)
  ) {
    return {
      trackerUnavailableReason: hasTrackers
        ? undefined
        : capabilitiesLoading
          ? "正在检查 AI 追踪能力"
          : "已连接的 AI 后端不支持视频追踪，请在项目设置 → AI 配置中接入追踪后端",
    };
  }
  if (backendsLoading) reason = "正在加载项目 AI 后端";
  else if (backendsError) reason = "项目 AI 后端加载失败，请刷新重试";
  else if (backends.length === 0)
    reason = "项目尚未接入 AI 后端，请到项目设置 → AI 配置中接入并启用";
  else if (capabilitiesLoading) reason = "正在检查 AI 后端能力";
  else reason = "AI 后端连接或能力检查失败，请到项目设置 → AI 配置中检查连接";
  return { aiUnavailableReason: reason, trackerUnavailableReason: reason };
}
