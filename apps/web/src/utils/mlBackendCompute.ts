export interface MLBackendCompute {
  configured_device?: string | null;
  effective_device?: string | null;
  effective_provider?: string | null;
  cpu_fallback_supported?: boolean | null;
}

interface RuntimeComputeObservation {
  status_code?: number | null;
  ok?: boolean;
  compute?: MLBackendCompute | null;
}

function normalized(value: string | null | undefined): string {
  return value?.trim().toLowerCase() ?? "";
}

export function isCpuFallback(compute: MLBackendCompute | null | undefined): boolean {
  if (!compute || compute.cpu_fallback_supported === false) return false;

  const configured = normalized(compute.configured_device);
  const configuredForGpu =
    configured === "gpu" || configured === "cuda" || configured.startsWith("cuda:");
  if (!configuredForGpu) return false;

  return (
    normalized(compute.effective_device) === "cpu" ||
    normalized(compute.effective_provider) === "cpuexecutionprovider"
  );
}

export function resolveRuntimeCompute(
  observation: RuntimeComputeObservation | undefined,
  snapshot: MLBackendCompute | null | undefined,
): MLBackendCompute | null | undefined {
  return observation?.status_code === 200 ? observation.compute : snapshot;
}
