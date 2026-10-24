import { pointCloudManifestQueryKey } from "@/api/pointCloudOriginals";
import { useOriginalAssetOwner } from "./useOriginalAssetOwner";
import { useQuery } from "@tanstack/react-query";

import { tasksApi } from "@/api/tasks";
import {
  ensurePointCloudNavigationGeneration,
  publishPointCloudNavigationTrace,
  registerPointCloudNavigationResource,
} from "@/utils/pointCloudNavigationDiagnostics";

/** Load metadata and versioned original identities for the active 3D task. */
export function usePointCloudManifest(taskId: string | null, enabled: boolean) {
  useOriginalAssetOwner();
  return useQuery({
    queryKey: pointCloudManifestQueryKey(taskId),
    queryFn: async ({ signal }) => {
      const requestedTaskId = taskId!;
      const generation = ensurePointCloudNavigationGeneration(requestedTaskId, "manifest");
      let signalledAbort = signal.aborted;
      const handleAbort = () => {
        signalledAbort = true;
        publishPointCloudNavigationTrace({
          source: "manifest",
          type: "signal-abort",
          generation,
          taskId: requestedTaskId,
          targetTaskId: requestedTaskId,
          status: "aborted",
          pending: true,
        });
      };
      signal.addEventListener("abort", handleAbort, { once: true });
      publishPointCloudNavigationTrace({
        source: "manifest",
        type: "request-start",
        generation,
        taskId: requestedTaskId,
        targetTaskId: requestedTaskId,
        status: "pending",
        pending: true,
      });
      try {
        const manifest = await tasksApi.getPointCloudManifest(requestedTaskId, { signal });
        const resourceKey = registerPointCloudNavigationResource({
          taskId: manifest.task_id,
          frameIndex: manifest.frame_index,
          url: manifest.point_cloud_url ?? "",
          kind: "point-cloud",
        });
        for (const camera of manifest.cameras) {
          registerPointCloudNavigationResource({
            taskId: manifest.task_id,
            frameIndex: manifest.frame_index,
            url: camera.image_url ?? "",
            kind: "camera",
            cameraRole: camera.role || camera.name,
          });
        }
        publishPointCloudNavigationTrace({
          source: "manifest",
          type: signalledAbort ? "response-after-abort" : "request-success",
          generation,
          taskId: requestedTaskId,
          targetTaskId: requestedTaskId,
          resolvedTaskId: manifest.task_id,
          frameIndex: manifest.frame_index,
          resourceKey,
          cameraCount: manifest.cameras.length,
          status: signalledAbort ? "stale-success" : "success",
          pending: false,
        });
        return manifest;
      } catch (error) {
        publishPointCloudNavigationTrace({
          source: "manifest",
          type: signalledAbort ? "request-aborted" : "request-error",
          generation,
          taskId: requestedTaskId,
          targetTaskId: requestedTaskId,
          status: signalledAbort
            ? "aborted"
            : error instanceof Error
              ? error.name
              : "unknown-error",
          pending: false,
        });
        throw error;
      } finally {
        signal.removeEventListener("abort", handleAbort);
      }
    },
    enabled: enabled && !!taskId,
    // Reuse metadata briefly; each uncached binary read still checks its version.
    staleTime: 5 * 60 * 1000,
  });
}
