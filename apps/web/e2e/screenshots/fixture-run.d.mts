export function cleanupScreenshotRun(runId: string): Promise<void>;
export function prepareScreenshotCapture(runId: string, apiPort: number): Promise<void>;
export function seedScreenshotCapture(
  runId: string,
  apiOrigin: string,
  arguments_: string[],
): Promise<void>;
export function requireScreenshotCaptureClosed(runId: string): void;
export function screenshotCaptureWorkers(runId: string): string[];
