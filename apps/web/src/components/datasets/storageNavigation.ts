const DATASET_PATH =
  /^\/datasets(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$/i;

export function safeDatasetReturn(value: string | null | undefined): string | null {
  if (!value || value.length > 8000 || !value.startsWith("/") || value.includes("\\")) return null;
  try {
    const url = new URL(value, "https://dataset.invalid");
    if (url.origin !== "https://dataset.invalid" || !DATASET_PATH.test(url.pathname)) return null;
    return `${url.pathname}${url.search}`;
  } catch {
    return null;
  }
}

export function storageLocationLink(locationId: string, returnTo: string): string {
  const params = new URLSearchParams({ tab: "locations", location: locationId });
  const safeReturn = safeDatasetReturn(returnTo);
  if (safeReturn) params.set("returnTo", safeReturn);
  return `/storage?${params}`;
}

export interface ImportTextDraft {
  mode: "files" | "zip" | "connection";
  name: string;
  nameTouched: boolean;
  description: string;
  dataType: string;
  axisConvention: string;
  isTemporal: boolean;
  connectionId: string;
  sourcePath: string;
  recursive: boolean;
  includeGlobs: string;
}

type DraftSource = "current" | "legacy";

export interface StoredImportTextDraft {
  source: DraftSource;
  raw: string;
  draft: ImportTextDraft | null;
}

function draftKey(source: DraftSource, session: string, datasetId?: string) {
  const prefix = source === "current" ? "datasets" : "aap";
  return `${prefix}:import-text:${encodeURIComponent(session)}:${datasetId ?? "new"}`;
}

const stringFields = [
  "name",
  "description",
  "dataType",
  "axisConvention",
  "connectionId",
  "sourcePath",
  "includeGlobs",
] as const;
const booleanFields = ["nameTouched", "isTemporal", "recursive"] as const;
const axes = [
  "iso_8855",
  "ros_rep103",
  "kitti_camera",
  "opencv_camera",
  "apollo",
  "y_forward",
  "sustechpoints_demo",
  "raw",
];

function parseDraft(raw: string): ImportTextDraft | null {
  if (raw.length > 20000) return null;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    const data = value as Record<string, unknown>;
    const fields = ["mode", ...stringFields, ...booleanFields];
    if (
      Object.keys(data).some((key) => !fields.includes(key)) ||
      !stringFields.every((key) => typeof data[key] === "string" && data[key].length <= 4000) ||
      !booleanFields.every((key) => typeof data[key] === "boolean") ||
      !["files", "zip", "connection"].includes(data.mode as string) ||
      !["image", "video", "point_cloud", "multimodal", "other"].includes(data.dataType as string) ||
      !axes.includes(data.axisConvention as string)
    )
      return null;
    return data as unknown as ImportTextDraft;
  } catch {
    return null;
  }
}

export function inspectImportTextDrafts(
  session: string | null,
  datasetId?: string,
): StoredImportTextDraft[] {
  if (!session) return [];
  return (["current", "legacy"] as const).flatMap((source) => {
    const raw = sessionStorage.getItem(draftKey(source, session, datasetId));
    return raw === null ? [] : [{ source, raw, draft: parseDraft(raw) }];
  });
}

export function saveImportTextDraft(
  session: string | null,
  datasetId: string | undefined,
  draft: ImportTextDraft,
) {
  if (!session) throw new Error("没有可用的登录会话");
  if (inspectImportTextDrafts(session, datasetId).length) throw new Error("请先处理已有的导入草稿");
  const {
    mode,
    name,
    nameTouched,
    description,
    dataType,
    axisConvention,
    isTemporal,
    connectionId,
    sourcePath,
    recursive,
    includeGlobs,
  } = draft;
  const raw = JSON.stringify({
    mode,
    name,
    nameTouched,
    description,
    dataType,
    axisConvention,
    isTemporal,
    connectionId,
    sourcePath,
    recursive,
    includeGlobs,
  });
  if (!parseDraft(raw)) throw new Error("导入草稿无法安全保存，请检查输入长度和内容");
  sessionStorage.setItem(draftKey("current", session, datasetId), raw);
}

export function upgradeImportTextDraft(
  session: string,
  datasetId: string | undefined,
  record: StoredImportTextDraft,
): StoredImportTextDraft {
  if (record.source !== "legacy" || !record.draft) throw new Error("旧版草稿无法升级");
  const currentKey = draftKey("current", session, datasetId);
  if (
    sessionStorage.getItem(draftKey("legacy", session, datasetId)) !== record.raw ||
    sessionStorage.getItem(currentKey) !== null
  )
    throw new Error("草稿已更改或当前草稿尚未处理");
  sessionStorage.setItem(currentKey, record.raw);
  if (sessionStorage.getItem(currentKey) !== record.raw) throw new Error("无法核验新草稿");
  removeImportTextDraft(session, datasetId, record);
  return { ...record, source: "current" };
}

export function removeImportTextDraft(
  session: string,
  datasetId: string | undefined,
  record: StoredImportTextDraft,
) {
  const key = draftKey(record.source, session, datasetId);
  if (sessionStorage.getItem(key) !== record.raw) throw new Error("草稿已更改，请重新检查");
  sessionStorage.removeItem(key);
}
