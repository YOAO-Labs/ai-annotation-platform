import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { tasksApi } from "@/api/tasks";
import type {
  SensorCalibrationHistoryOut,
  SensorCalibrationUpdate,
  TaskPointCloudManifestResponse,
} from "@/api/generated";
import { POINT_CLOUD_QUALITY_QUERY_KEY } from "@/hooks/usePointCloudQuality";

export function sensorCalibrationKey(taskId: string | null, cameraRole: string | null) {
  return ["sensor-calibration", taskId, cameraRole] as const;
}

export function useSensorCalibration({
  taskId,
  cameraRole,
  projectId,
  enabled,
}: {
  taskId: string | null;
  cameraRole: string | null;
  projectId: string | null;
  enabled: boolean;
}) {
  const queryClient = useQueryClient();
  const queryKey = sensorCalibrationKey(taskId, cameraRole);
  const query = useQuery({
    queryKey,
    queryFn: ({ signal }) => tasksApi.getSensorCalibrationHistory(taskId!, cameraRole!, { signal }),
    enabled: enabled && !!taskId && !!cameraRole,
  });
  useEffect(() => {
    const latest = query.data?.items[0];
    if (!latest) return;
    queryClient.setQueryData<TaskPointCloudManifestResponse>(
      ["task-point-cloud-manifest", taskId],
      (current) =>
        current
          ? {
              ...current,
              cameras: current.cameras.map((camera) =>
                camera.role === cameraRole
                  ? {
                      ...camera,
                      calibration: latest.calibration,
                      calibration_revision: latest.revision,
                      calibration_digest: latest.digest,
                    }
                  : camera,
              ),
            }
          : current,
    );
  }, [cameraRole, query.data, queryClient, taskId]);
  const refreshRelated = async () => {
    const results = await Promise.allSettled([
      queryClient.invalidateQueries({ queryKey }, { throwOnError: true }),
      queryClient.invalidateQueries(
        { queryKey: ["task-point-cloud-manifest", taskId] },
        { throwOnError: true },
      ),
      queryClient.invalidateQueries(
        {
          predicate: (candidate) =>
            candidate.queryKey[0] === "camera-annotation-members" &&
            candidate.queryKey[1] === taskId,
        },
        { throwOnError: true },
      ),
      projectId
        ? queryClient.invalidateQueries(
            { queryKey: [POINT_CLOUD_QUALITY_QUERY_KEY, projectId] },
            { throwOnError: true },
          )
        : Promise.resolve(),
    ]);
    return results.every((result) => result.status === "fulfilled");
  };
  const update = useMutation({
    mutationFn: ({
      payload,
      idempotencyKey,
    }: {
      payload: SensorCalibrationUpdate;
      idempotencyKey: string;
    }) => tasksApi.updateSensorCalibration(taskId!, cameraRole!, payload, idempotencyKey),
    onSuccess: (updated) => {
      queryClient.setQueryData<SensorCalibrationHistoryOut>(queryKey, (current) => ({
        current_revision: updated.revision,
        current_digest: updated.digest,
        items: [
          updated,
          ...(current?.items.filter((item) => item.revision !== updated.revision) ?? []),
        ],
      }));
      queryClient.setQueryData<TaskPointCloudManifestResponse>(
        ["task-point-cloud-manifest", taskId],
        (current) =>
          current
            ? {
                ...current,
                cameras: current.cameras.map((camera) =>
                  camera.role === cameraRole
                    ? {
                        ...camera,
                        calibration: updated.calibration,
                        calibration_revision: updated.revision,
                        calibration_digest: updated.digest,
                      }
                    : camera,
                ),
              }
            : current,
      );
    },
  });

  return { query, update, refreshRelated };
}
