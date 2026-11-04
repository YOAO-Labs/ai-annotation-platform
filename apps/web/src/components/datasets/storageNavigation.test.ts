import { beforeEach, expect, it } from "vitest";
import {
  safeDatasetReturn,
  inspectImportTextDrafts,
  removeImportTextDraft,
  saveImportTextDraft,
  storageLocationLink,
} from "./storageNavigation";

beforeEach(() => sessionStorage.clear());
it("only returns to dataset surfaces while retaining filter/source/scan context", () => {
  const target =
    "/datasets/11111111-1111-1111-1111-111111111111?tab=sources&source=s&scan=r&page=3";
  expect(safeDatasetReturn(target)).toBe(target);
  for (const value of [
    "https://evil.test/datasets",
    "//evil.test/datasets",
    "/\\evil.test/datasets",
    "/settings",
    "/datasets/../settings",
    "/datasets/not-an-id",
  ])
    expect(safeDatasetReturn(value)).toBeNull();
  const link = new URL(storageLocationLink("location", target), "https://local.test");
  expect(link.searchParams.get("returnTo")).toBe(target);
  expect(link.searchParams.get("location")).toBe("location");
});

it("isolates single-use non-secret drafts by session and dataset and never persists Files", () => {
  const draft = {
    mode: "connection" as const,
    name: "Vehicles",
    nameTouched: true,
    description: "text",
    dataType: "image",
    axisConvention: "iso_8855",
    isTemporal: false,
    connectionId: "connection",
    sourcePath: "fleet/",
    recursive: true,
    includeGlobs: "*.jpg",
    files: [new File(["bytes"], "secret.png")],
    secret: "credential",
  };
  saveImportTextDraft("session-a", "dataset-a", draft);
  expect(inspectImportTextDrafts("session-b", "dataset-a")).toEqual([]);
  expect(inspectImportTextDrafts("session-a", "dataset-b")).toEqual([]);
  expect(sessionStorage.getItem("datasets:import-text:session-a:dataset-a")).not.toBeNull();
  const raw = Object.values(sessionStorage).join("");
  expect(raw).not.toContain("secret.png");
  expect(raw).not.toContain("credential");
  const [record] = inspectImportTextDrafts("session-a", "dataset-a");
  expect(record.draft).toEqual({
    mode: "connection",
    name: "Vehicles",
    nameTouched: true,
    description: "text",
    dataType: "image",
    axisConvention: "iso_8855",
    isTemporal: false,
    connectionId: "connection",
    sourcePath: "fleet/",
    recursive: true,
    includeGlobs: "*.jpg",
  });
  removeImportTextDraft("session-a", "dataset-a", record);
  expect(inspectImportTextDrafts("session-a", "dataset-a")).toEqual([]);
});

it("retains corrupt, newer and conflicting records and never removes replacement bytes", () => {
  const current = "datasets:import-text:session-a:new";
  const legacy = "aap:import-text:session-a:new";
  sessionStorage.setItem(current, '{"version":2,"name":"future"}');
  sessionStorage.setItem(legacy, "{broken");
  const records = inspectImportTextDrafts("session-a");
  expect(records.map((record) => record.draft)).toEqual([null, null]);
  expect(sessionStorage.getItem(current)).toBe('{"version":2,"name":"future"}');
  expect(sessionStorage.getItem(legacy)).toBe("{broken");
  expect(() => saveImportTextDraft("session-a", undefined, {} as never)).toThrow();
  sessionStorage.setItem(current, "replacement");
  expect(() => removeImportTextDraft("session-a", undefined, records[0])).toThrow();
  expect(sessionStorage.getItem(current)).toBe("replacement");
  expect(sessionStorage.getItem(legacy)).toBe("{broken");
});
