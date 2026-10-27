const DATASET_PATH =
  /^\/datasets(?:\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?$/i;

/** Restrict configuration return links to the dataset surfaces that own drafts. */
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

function draftKey(session: string, datasetId?: string) {
  return `aap:import-text:${encodeURIComponent(session)}:${datasetId ?? "new"}`;
}

export function saveImportTextDraft(
  session: string | null,
  datasetId: string | undefined,
  draft: ImportTextDraft,
) {
  if (!session) return;
  try {
    // Select fields explicitly: files, credentials and job payloads never enter storage.
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
    sessionStorage.setItem(
      draftKey(session, datasetId),
      JSON.stringify({
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
      }),
    );
  } catch {
    /* Private browsing or full storage must not block configuration. */
  }
}

export function takeImportTextDraft(
  session: string | null,
  datasetId?: string,
): Partial<ImportTextDraft> | null {
  if (!session) return null;
  try {
    const key = draftKey(session, datasetId);
    const raw = sessionStorage.getItem(key);
    sessionStorage.removeItem(key);
    if (!raw || raw.length > 20000) return null;
    const data = JSON.parse(raw) as Record<string, unknown>;
    const draft: Partial<ImportTextDraft> = {};
    for (const key of [
      "name",
      "description",
      "dataType",
      "axisConvention",
      "connectionId",
      "sourcePath",
      "includeGlobs",
    ] as const) {
      if (typeof data[key] === "string") draft[key] = data[key].slice(0, 4000);
    }
    for (const key of ["nameTouched", "isTemporal", "recursive"] as const) {
      if (typeof data[key] === "boolean") draft[key] = data[key];
    }
    if (["files", "zip", "connection"].includes(data.mode as string))
      draft.mode = data.mode as ImportTextDraft["mode"];
    return draft;
  } catch {
    return null;
  }
}
