const MAX_EVENTS = 320;

export type PointCloudNavigationTraceSource =
  | "timeline"
  | "shell"
  | "manifest"
  | "point-cloud"
  | "camera";

export interface PointCloudNavigationTraceEvent {
  sequence: number;
  elapsedMs: number;
  generation: number | null;
  source: PointCloudNavigationTraceSource;
  type: string;
  taskId?: string;
  targetTaskId?: string;
  currentTaskId?: string | null;
  requestedTaskId?: string | null;
  resolvedTaskId?: string | null;
  frameIndex?: number | null;
  resourceKey?: string;
  cameraRole?: string;
  cameraCount?: number;
  status?: string;
  allowed?: boolean;
  pending?: boolean;
}

export type PointCloudNavigationTraceEventInput = Omit<
  PointCloudNavigationTraceEvent,
  "sequence" | "elapsedMs" | "generation"
> & {
  generation?: number | null;
};

export interface PointCloudNavigationTraceSnapshot {
  route: string;
  startedAt: string;
  updatedAt: string;
  activeGeneration: number | null;
  events: PointCloudNavigationTraceEvent[];
}

interface TrackedResource {
  generation: number;
  taskId: string;
  frameIndex: number | null;
  kind: "point-cloud" | "camera";
  cameraRole?: string;
}

interface PointCloudNavigationTraceStore extends PointCloudNavigationTraceSnapshot {
  startedAtMs: number;
  nextEventSequence: number;
  nextGeneration: number;
  taskGenerations: Record<string, number>;
  resources: Record<string, TrackedResource>;
}

interface DiagnosticsWindow {
  __pointCloudNavigationTrace?: PointCloudNavigationTraceStore;
  __pointCloudNavigationTraceListenerInstalled?: boolean;
  __exportPointCloudNavigationTrace?: () => string;
  __downloadPointCloudNavigationTrace?: () => void;
}

function diagnosticsEnabled(): boolean {
  return import.meta.env.DEV && typeof window !== "undefined";
}

function diagnosticsWindow(): (Window & DiagnosticsWindow) | null {
  if (!diagnosticsEnabled()) return null;
  return window as Window & DiagnosticsWindow;
}

function monotonicNow(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

function currentRoute(): string {
  return typeof window === "undefined" ? "" : window.location.pathname;
}

function ensureStore(): PointCloudNavigationTraceStore | null {
  const target = diagnosticsWindow();
  if (!target) return null;
  const route = currentRoute();
  if (!target.__pointCloudNavigationTrace || target.__pointCloudNavigationTrace.route !== route) {
    const now = new Date().toISOString();
    target.__pointCloudNavigationTrace = {
      route,
      startedAt: now,
      updatedAt: now,
      startedAtMs: monotonicNow(),
      activeGeneration: null,
      nextEventSequence: 1,
      nextGeneration: 1,
      taskGenerations: {},
      resources: {},
      events: [],
    };
  }
  installGlobalDiagnostics(target);
  return target.__pointCloudNavigationTrace;
}

function snapshotFromStore(
  store: PointCloudNavigationTraceStore,
): PointCloudNavigationTraceSnapshot {
  return {
    route: store.route,
    startedAt: store.startedAt,
    updatedAt: store.updatedAt,
    activeGeneration: store.activeGeneration,
    events: store.events.map((event) => ({ ...event })),
  };
}

function installGlobalDiagnostics(target: Window & DiagnosticsWindow): void {
  target.__exportPointCloudNavigationTrace = () =>
    JSON.stringify(getPointCloudNavigationTraceSnapshot(), null, 2);
  target.__downloadPointCloudNavigationTrace = () => {
    const json = target.__exportPointCloudNavigationTrace?.() ?? "{}";
    const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "point-cloud-navigation-trace.json";
    anchor.click();
    URL.revokeObjectURL(url);
  };
  if (target.__pointCloudNavigationTraceListenerInstalled) return;
  target.__pointCloudNavigationTraceListenerInstalled = true;
  const recordImageResult = (event: Event) => {
    if (!(event.target instanceof HTMLImageElement)) return;
    recordPointCloudCameraResourceResult(
      event.target.currentSrc || event.target.src,
      event.type === "load" ? "load" : "error",
    );
  };
  target.addEventListener("load", recordImageResult, true);
  target.addEventListener("error", recordImageResult, true);
}

export function fingerprintPointCloudResource(value: string | null | undefined): string {
  if (!value) return "resource-none";
  let hash = 0x811c9dc5;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `resource-${(hash >>> 0).toString(16).padStart(8, "0")}`;
}

export function publishPointCloudNavigationTrace(input: PointCloudNavigationTraceEventInput): void {
  const store = ensureStore();
  if (!store) return;
  const generation =
    input.generation ??
    (input.taskId ? store.taskGenerations[input.taskId] : undefined) ??
    store.activeGeneration;
  const event: PointCloudNavigationTraceEvent = {
    ...input,
    generation: generation ?? null,
    sequence: store.nextEventSequence++,
    elapsedMs: Math.round((monotonicNow() - store.startedAtMs) * 10) / 10,
  };
  store.events.push(event);
  if (store.events.length > MAX_EVENTS) {
    store.events.splice(0, store.events.length - MAX_EVENTS);
  }
  store.updatedAt = new Date().toISOString();
}

export function beginPointCloudNavigationTrace(input: {
  source: "timeline" | "shell";
  targetTaskId: string;
  frameIndex?: number | null;
  type?: string;
}): number {
  const store = ensureStore();
  if (!store) return 0;
  const generation = store.nextGeneration++;
  store.activeGeneration = generation;
  store.taskGenerations[input.targetTaskId] = generation;
  publishPointCloudNavigationTrace({
    source: input.source,
    type: input.type ?? "navigation-intent",
    generation,
    targetTaskId: input.targetTaskId,
    taskId: input.targetTaskId,
    frameIndex: input.frameIndex,
  });
  return generation;
}

export function ensurePointCloudNavigationGeneration(
  taskId: string,
  source: "shell" | "manifest" | "point-cloud",
  frameIndex?: number | null,
): number {
  const store = ensureStore();
  if (!store) return 0;
  const existing = store.taskGenerations[taskId];
  if (existing !== undefined) return existing;
  return beginPointCloudNavigationTrace({
    source: "shell",
    targetTaskId: taskId,
    frameIndex,
    type: `${source}-generation-created`,
  });
}

export function pointCloudNavigationGenerationForTask(taskId: string | null | undefined) {
  if (!taskId) return null;
  return ensureStore()?.taskGenerations[taskId] ?? null;
}

export function registerPointCloudNavigationResource(input: {
  taskId: string;
  frameIndex?: number | null;
  url: string;
  kind: "point-cloud" | "camera";
  cameraRole?: string;
}): string {
  const store = ensureStore();
  if (!store) return "resource-disabled";
  const resourceKey = fingerprintPointCloudResource(input.url);
  const generation = ensurePointCloudNavigationGeneration(
    input.taskId,
    input.kind === "point-cloud" ? "point-cloud" : "manifest",
    input.frameIndex,
  );
  store.resources[resourceKey] = {
    generation,
    taskId: input.taskId,
    frameIndex: input.frameIndex ?? null,
    kind: input.kind,
    cameraRole: input.cameraRole,
  };
  return resourceKey;
}

export function recordPointCloudCameraResourceResult(url: string, status: "load" | "error"): void {
  const store = ensureStore();
  const resourceKey = fingerprintPointCloudResource(url);
  const tracked = store?.resources[resourceKey];
  if (!tracked || tracked.kind !== "camera") return;
  publishPointCloudNavigationTrace({
    source: "camera",
    type: status === "load" ? "image-load" : "image-error",
    generation: tracked.generation,
    taskId: tracked.taskId,
    frameIndex: tracked.frameIndex,
    resourceKey,
    cameraRole: tracked.cameraRole,
    status,
  });
}

export function publishPointCloudResourceTrace(
  url: string,
  input: {
    type: string;
    status?: string;
    pending?: boolean;
  },
): void {
  const store = ensureStore();
  if (!store) return;
  const resourceKey = fingerprintPointCloudResource(url);
  const tracked = store.resources[resourceKey];
  if (!tracked || tracked.kind !== "point-cloud") return;
  publishPointCloudNavigationTrace({
    source: "point-cloud",
    type: input.type,
    generation: tracked.generation,
    taskId: tracked.taskId,
    frameIndex: tracked.frameIndex,
    resourceKey,
    status: input.status,
    pending: input.pending,
  });
}

export function getPointCloudNavigationTraceSnapshot(): PointCloudNavigationTraceSnapshot | null {
  const store = ensureStore();
  return store ? snapshotFromStore(store) : null;
}

export function resetPointCloudNavigationTraceForTests(): void {
  const target = diagnosticsWindow();
  if (target) delete target.__pointCloudNavigationTrace;
}
