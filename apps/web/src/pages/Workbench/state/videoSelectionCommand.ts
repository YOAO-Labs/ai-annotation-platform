export interface VideoSelectionCommandOptions {
  source?: "task-reset";
  shift?: boolean;
  frameIndex?: number;
  activateTrackTool?: boolean;
  onAdmitted?: () => void;
}

export type VideoSelectionCommand = (
  id: string | null,
  options?: VideoSelectionCommandOptions,
) => void;
