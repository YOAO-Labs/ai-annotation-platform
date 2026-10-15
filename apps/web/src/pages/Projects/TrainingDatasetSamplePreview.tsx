import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { trainingDatasetsApi, type TrainingDatasetAnalyzedSampleOut } from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";

type Props = {
  projectId: string;
  sample: TrainingDatasetAnalyzedSampleOut;
  onClose: () => void;
  onObserved: () => void;
};

export function TrainingDatasetSamplePreview(props: Props) {
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const owner = useMemo(
    () => ({
      userId,
      token,
      projectId: props.projectId,
      versionId: props.sample.version_id,
      taskId: props.sample.task_id,
      key: crypto.randomUUID(),
    }),
    [userId, token, props.projectId, props.sample.version_id, props.sample.task_id],
  );
  return <SamplePreviewContent key={owner.key} {...props} owner={owner} />;
}

function SamplePreviewContent({
  projectId,
  sample,
  onClose,
  onObserved,
  owner,
}: Props & { owner: { userId: string; token: string; key: string } }) {
  const [mediaUrl, setMediaUrl] = useState("");
  const [mediaError, setMediaError] = useState("");
  const [mediaLoading, setMediaLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [page, setPage] = useState(0);
  const [boxesVisible, setBoxesVisible] = useState(true);
  const detail = useQuery({
    queryKey: [
      "training-data-sample",
      owner.key,
      projectId,
      sample.version_id,
      sample.task_id,
      page,
    ],
    queryFn: ({ signal }) =>
      trainingDatasetsApi.sample(projectId, sample.version_id, sample.task_id, page * 500, {
        signal,
      }),
    retry: false,
  });
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let createdUrl = "";
    const owns = () =>
      active &&
      useAuthStore.getState().user?.id === owner.userId &&
      useAuthStore.getState().token === owner.token &&
      localStorage.getItem("token") === owner.token;
    setMediaUrl("");
    setMediaError("");
    setMediaLoading(true);
    void trainingDatasetsApi
      .sampleMedia(projectId, sample.version_id, sample.task_id, { signal: controller.signal })
      .then((blob) => {
        if (!owns()) return;
        createdUrl = URL.createObjectURL(blob);
        setMediaUrl(createdUrl);
      })
      .catch((reason: unknown) => {
        if (owns() && !controller.signal.aborted)
          setMediaError(reason instanceof Error ? reason.message : "冻结图片读取失败");
      })
      .finally(() => {
        if (owns()) {
          setMediaLoading(false);
          onObserved();
        }
      });
    return () => {
      active = false;
      controller.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [owner, projectId, sample.version_id, sample.task_id, retry, onObserved]);
  const value = detail.data;
  const polygons = value?.labels.some((label) => label.type === "polygon") ?? false;
  return (
    <Modal open onClose={onClose} title="冻结样本预览" width={900}>
      <p className="mb-3 break-all text-xs text-muted-foreground">
        样本 {sample.task_id} · {sample.split === "val" ? "验证集" : "训练集"} ·{" "}
        {sample.width ?? "未知"} × {sample.height ?? "未知"}
      </p>
      <p className="mb-4 text-xs text-muted-foreground">
        图片经冻结身份与字节校验后生成预览；实例来自训练包对应的冻结标注。预览按比例缩小。
      </p>
      {mediaLoading && (
        <p role="status" className="py-8 text-center text-sm text-muted-foreground">
          正在读取并校验冻结图片…
        </p>
      )}
      {mediaError && (
        <div
          role="alert"
          className="rounded-md bg-status-caution-soft p-3 text-sm text-status-caution"
        >
          <p>{mediaError}</p>
          <Button size="sm" className="mt-2" onClick={() => setRetry((value) => value + 1)}>
            重试冻结图片
          </Button>
        </div>
      )}
      {mediaUrl && (
        <div className="relative mx-auto w-fit max-w-full overflow-hidden rounded-md border border-border bg-muted">
          <img
            src={mediaUrl}
            alt={`冻结样本 ${sample.task_id.slice(0, 8)}`}
            className="block max-h-[60vh] max-w-full"
            onError={() => {
              setMediaError("浏览器无法解码冻结图片预览，请重试或检查持久存储");
              setMediaUrl("");
            }}
          />
          {boxesVisible && value && !value.sample.issue && (
            <svg
              className="pointer-events-none absolute inset-0 size-full text-status-info"
              viewBox="0 0 1 1"
              preserveAspectRatio="none"
              aria-label={polygons ? "冻结多边形" : "冻结矩形框"}
              role="img"
            >
              {value.labels.map((box, index) =>
                box.type === "polygon" ? (
                  <polygon
                    key={index}
                    points={box.points.map((point) => point.join(",")).join(" ")}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.5}
                    vectorEffect="non-scaling-stroke"
                  />
                ) : (
                  <rect
                    key={index}
                    x={box.x_center - box.width / 2}
                    y={box.y_center - box.height / 2}
                    width={box.width}
                    height={box.height}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth={1.5}
                    vectorEffect="non-scaling-stroke"
                  />
                ),
              )}
            </svg>
          )}
        </div>
      )}
      {detail.isLoading && (
        <p role="status" className="mt-3 text-xs text-muted-foreground">
          正在读取冻结标注…
        </p>
      )}
      {detail.error && (
        <p role="alert" className="mt-3 text-sm text-status-danger">
          {detail.error.message}
        </p>
      )}
      {value?.sample.issue && (
        <p
          role="alert"
          className="mt-3 rounded-md bg-status-caution-soft p-3 text-xs text-status-caution"
        >
          冻结标注未保留或与训练包不一致，不能将此样本视为空标注。
        </p>
      )}
      {value?.total === 0 && (
        <p className="mt-3 text-sm text-muted-foreground">
          {value.sample.explicit_negative
            ? "此样本已明确确认为无目标。"
            : "训练包中没有实例；历史负样本确认信息未保留。"}
        </p>
      )}
      {value?.total != null && value.total > 0 && (
        <>
          <div className="mt-4 flex flex-wrap items-center justify-between gap-2 text-xs">
            <label className="inline-flex items-center gap-2">
              <input
                type="checkbox"
                checked={boxesVisible}
                onChange={(event) => setBoxesVisible(event.target.checked)}
              />
              {polygons ? "显示本页冻结多边形" : "显示本页冻结框"}
            </label>
            <span className="text-muted-foreground">
              当前显示 {page * 500 + 1}–{page * 500 + value.labels.length} / {value.total} 个实例
            </span>
          </div>
          <div className="mt-2 max-h-40 overflow-auto">
            <table className="w-full text-left text-xs">
              <thead className="text-muted-foreground">
                <tr>
                  <th className="py-2 font-medium">冻结类别</th>
                  <th className="py-2 font-medium">{polygons ? "顶点数量" : "中心位置"}</th>
                  <th className="py-2 font-medium">
                    {polygons ? "面积占比 / 来源标注" : "宽 × 高（归一化）"}
                  </th>
                </tr>
              </thead>
              <tbody>
                {value.labels.map((box, index) => (
                  <tr key={index} className="border-t border-border">
                    <td className="py-2">{box.class_name}</td>
                    <td className="py-2 tabular-nums">
                      {box.type === "polygon"
                        ? `${box.points.length} 个顶点`
                        : `${box.x_center.toFixed(4)}, ${box.y_center.toFixed(4)}`}
                    </td>
                    <td className="py-2 tabular-nums">
                      {box.type === "polygon" ? (
                        <span title={box.annotation_id}>
                          {(box.area * 100).toFixed(2)}% · {box.annotation_id.slice(0, 8)} ·
                          标注版本 {box.annotation_version}
                        </span>
                      ) : (
                        `${box.width.toFixed(4)} × ${box.height.toFixed(4)}`
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {value.total > 500 && (
            <div className="mt-2 flex justify-end gap-2">
              <Button
                size="sm"
                disabled={!page || detail.isFetching}
                onClick={() => setPage((p) => p - 1)}
              >
                上一页标注
              </Button>
              <Button
                size="sm"
                disabled={(page + 1) * 500 >= value.total || detail.isFetching}
                onClick={() => setPage((p) => p + 1)}
              >
                下一页标注
              </Button>
            </div>
          )}
        </>
      )}
      <div className="mt-4 flex justify-end">
        <Button size="sm" onClick={onClose}>
          关闭预览
        </Button>
      </div>
    </Modal>
  );
}
