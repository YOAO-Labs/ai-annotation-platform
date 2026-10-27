import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskPointCloudManifestResponse } from "./generated";
const describeOriginal = vi.hoisted(() => vi.fn());
const authority = vi.hoisted(() => ({ current: true }));
vi.mock("./originals", () => ({
  captureOriginalOwner: () => ({ userId: "user", sessionId: "session" }),
  isOriginalOwnerCurrent: () => authority.current,
  originalOwnerKey: () => "user/session",
  describeOriginal,
}));
import { hydratePointCloudOriginals } from "./pointCloudOriginals";
const task = "11111111-1111-4111-8111-111111111111";
const link = "22222222-2222-4222-8222-222222222222";
function manifest() {
  return {
    task_id: task,
    point_cloud_url: "https://old.invalid/pcd?secret=1",
    point_cloud_format: "pcd",
    original_identity_path: `/api/v1/files/tasks/${task}/original-identity`,
    expires_in: 300,
    cameras: [
      {
        name: "front",
        role: "CAM_FRONT",
        dataset_item_id: task,
        link_id: link,
        original_identity_path: `/api/v1/files/tasks/${task}/links/${link}/original-identity`,
        image_url: "https://old.invalid/camera?secret=1",
        calibration: { intrinsic: [1] },
      },
    ],
  } as unknown as TaskPointCloudManifestResponse;
}
describe("point-cloud original hydration", () => {
  beforeEach(() => {
    authority.current = true;
    describeOriginal.mockReset();
  });
  it("uses typed task/link identities and preserves camera metadata if primary is unavailable", async () => {
    describeOriginal.mockImplementation(async (resource) => {
      if (resource.kind === "task") throw new Error("unpinned");
      return { download_path: "/api/v1/verified-camera?content_id=new&sha256=receipt" };
    });
    const value = await hydratePointCloudOriginals(manifest(), task);
    expect(value.point_cloud_url).toBeNull();
    expect(value.cameras[0]).toMatchObject({
      name: "front",
      calibration: { intrinsic: [1] },
      image_url: "/api/v1/verified-camera?content_id=new&sha256=receipt",
    });
    expect(describeOriginal).toHaveBeenCalledWith(
      { kind: "camera", taskId: task, linkId: link },
      expect.anything(),
    );
    expect(JSON.stringify(value)).not.toContain("old.invalid");
  });
  it("never resolves a mismatched path or accepts a legacy signed URL", async () => {
    const raw = manifest() as ReturnType<typeof manifest> & { original_identity_path: string };
    raw.original_identity_path = "/api/v1/files/tasks/another/original-identity";
    delete (raw.cameras[0] as { link_id?: string }).link_id;
    const value = await hydratePointCloudOriginals(raw, task);
    expect(value.point_cloud_url).toBeNull();
    expect(value.cameras[0].image_url).toBeNull();
    expect(describeOriginal).not.toHaveBeenCalled();
  });
  it("rejects mismatched tasks before identity I/O", async () => {
    await expect(hydratePointCloudOriginals(manifest(), link)).rejects.toThrow("identity_mismatch");
    expect(describeOriginal).not.toHaveBeenCalled();
  });
  it("fences an ignored-abort identity completion after login changes", async () => {
    describeOriginal.mockImplementation(async () => {
      authority.current = false;
      return { download_path: "/old" };
    });
    await expect(hydratePointCloudOriginals(manifest(), task)).rejects.toMatchObject({
      name: "AbortError",
    });
  });
});
