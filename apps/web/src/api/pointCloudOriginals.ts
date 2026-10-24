import type { PointCloudCameraOut, TaskPointCloudManifestResponse } from "./generated";
import {
  captureOriginalOwner,
  describeOriginal,
  isOriginalOwnerCurrent,
  originalOwnerKey,
  type OriginalOwner,
  type OriginalResource,
} from "./originals";

export type HydratedPointCloudManifest = TaskPointCloudManifestResponse & {
  original_error: string | null;
  cameras: (PointCloudCameraOut & { original_error: string | null })[];
};

export function pointCloudManifestQueryKey(taskId: string | null) {
  return ["task-point-cloud-manifest", originalOwnerKey(), taskId] as const;
}

function check(owner: OriginalOwner | null, signal?: AbortSignal) {
  if (signal?.aborted || !owner || !isOriginalOwnerCurrent(owner))
    throw new DOMException("Original owner changed", "AbortError");
}

/** Only typed, exact resources may replace the deliberately empty wire URLs. */
export async function hydratePointCloudOriginals(
  manifest: TaskPointCloudManifestResponse,
  taskId: string,
  owner: OriginalOwner | null = captureOriginalOwner(),
  signal?: AbortSignal,
): Promise<HydratedPointCloudManifest> {
  check(owner, signal);
  if (manifest.task_id !== taskId) throw new Error("point_cloud_manifest_identity_mismatch");
  const resource = async (path: unknown, expected: string, typed: OriginalResource) => {
    if (path !== expected) return null;
    try {
      const identity = await describeOriginal(typed, { owner: owner!, signal });
      check(owner, signal);
      return identity.download_path;
    } catch (error) {
      check(owner, signal);
      if (error instanceof DOMException && error.name === "AbortError") throw error;
      return null;
    }
  };
  const wire = manifest as TaskPointCloudManifestResponse & {
    original_identity_path?: string | null;
  };
  const [pointCloudUrl, cameras] = await Promise.all([
    resource(wire.original_identity_path, `/api/v1/files/tasks/${taskId}/original-identity`, {
      kind: "task",
      taskId,
    }),
    Promise.all(
      manifest.cameras.map(async (camera) => {
        const typed = camera as typeof camera & {
          link_id?: string | null;
          original_identity_path?: string | null;
        };
        const imageUrl = typed.link_id
          ? await resource(
              typed.original_identity_path,
              `/api/v1/files/tasks/${taskId}/links/${typed.link_id}/original-identity`,
              { kind: "camera", taskId, linkId: typed.link_id },
            )
          : null;
        return {
          ...camera,
          image_url: imageUrl,
          original_error: imageUrl ? null : "original_identity_unavailable",
        };
      }),
    ),
  ]);
  check(owner, signal);
  return {
    ...manifest,
    point_cloud_url: pointCloudUrl,
    cameras,
    expires_in: 0,
    original_error: pointCloudUrl ? null : "original_identity_unavailable",
  };
}
