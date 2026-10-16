export const RECORDING_FLOWS: Record<string, string[]>;
export const MARKETING_ONLY_FLOWS: string[];

export function recordingInference(flowId: string): "none" | "live";

export function recordingPlan(
  flows: string[],
  profile?: string,
): {
  flows: string[];
  profile: "docs" | "marketing";
  backendRequirements: string;
  grep: string;
};

export function screenshotCatalogPath(scope?: string): string;

export function assertRecordingMode(profile: "docs" | "marketing", validateOnly?: boolean): void;

export function recordingTimeout(requested: number, current: number, selected: boolean): number;
