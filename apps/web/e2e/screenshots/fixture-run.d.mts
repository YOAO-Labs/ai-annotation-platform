export function cleanupScreenshotRun(runId: string): Promise<void>;
export function prepareScreenshotCapture(runId: string, apiPort: number): Promise<void>;
export function seedScreenshotCapture(
  runId: string,
  apiOrigin: string,
  arguments_: string[],
  /** Abort cooperatively interrupts the owned Python seed and waits for its close. */
  signal?: AbortSignal,
): Promise<void>;
export function requireScreenshotCaptureClosed(runId: string): void;
export function screenshotCaptureWorkers(runId: string): string[];
