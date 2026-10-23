import { beforeEach, expect, it } from "vitest";
import {
  safeDatasetReturn,
  saveImportTextDraft,
  takeImportTextDraft,
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
  expect(takeImportTextDraft("session-b", "dataset-a")).toBeNull();
  expect(takeImportTextDraft("session-a", "dataset-b")).toBeNull();
  const raw = Object.values(sessionStorage).join("");
  expect(raw).not.toContain("secret.png");
  expect(raw).not.toContain("credential");
  expect(takeImportTextDraft("session-a", "dataset-a")).toEqual({
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
  expect(takeImportTextDraft("session-a", "dataset-a")).toBeNull();
});
