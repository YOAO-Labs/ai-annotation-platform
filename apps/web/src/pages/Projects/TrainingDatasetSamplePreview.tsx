import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  trainingDatasetsApi,
  type TrainingDatasetAnalyzedSampleOut,
  type TrainingDatasetInstanceMask,
  type TrainingDatasetSampleDetail,
} from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Modal } from "@/components/ui/Modal";
import { useTrainingSessionRevision } from "../Training/trainingViewOwner";

import { FrozenInstanceMaskPreview } from "./FrozenInstanceMaskPreview";
import { TrainingDatasetSourceLineage } from "./TrainingDatasetSourceLineage";

const isInstanceMask = (
  label: TrainingDatasetSampleDetail["labels"][number],
): label is TrainingDatasetInstanceMask => "pixel_sha256" in label && "size" in label;

type Props = {
  projectId: string;
  sample: TrainingDatasetAnalyzedSampleOut;
  onClose: () => void;
  onObserved: () => void;
};

type PanelProps = Omit<Props, "onClose">;
type PreviewOwner = { userId: string; token: string; revision: number; key: string };

function ownsPreview(owner: PreviewOwner) {
  return (
    getAuthSessionRevision() === owner.revision &&
    useAuthStore.getState().user?.id === owner.userId &&
    useAuthStore.getState().token === owner.token &&
    localStorage.getItem("token") === owner.token
  );
}

export function TrainingDatasetSamplePreview(props: Props) {
  const returnFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  return (
    <Modal
      open
      onClose={props.onClose}
      title="冻结样本预览"
      width={900}
      stopEscapePropagation
      returnFocusRef={returnFocusRef}
    >
      <TrainingDatasetSamplePanel
        projectId={props.projectId}
        sample={props.sample}
        onObserved={props.onObserved}
      />
      <div className="mt-4 flex justify-end">
        <Button size="sm" onClick={props.onClose}>
          关闭预览
        </Button>
      </div>
    </Modal>
  );
}

export function TrainingDatasetSamplePanel(props: PanelProps) {
  const revision = useTrainingSessionRevision();
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const owner = useMemo(
    () => ({
      userId,
      token,
      revision,
      projectId: props.projectId,
      versionId: props.sample.version_id,
      taskId: props.sample.task_id,
      contentSha256: props.sample.sha256,
      labelSha256: props.sample.label_sha256,
      key: randomId(),
    }),
    [
      userId,
      token,
      revision,
      props.projectId,
      props.sample.version_id,
      props.sample.task_id,
      props.sample.sha256,
      props.sample.label_sha256,
    ],
  );
  return <SamplePreviewContent key={owner.key} {...props} owner={owner} />;
}

function SamplePreviewContent({
  projectId,
  sample,
  onObserved,
  owner,
}: PanelProps & { owner: PreviewOwner }) {
  const observedRef = useRef(onObserved);
  useEffect(() => {
    observedRef.current = onObserved;
  }, [onObserved]);
  const [mediaUrl, setMediaUrl] = useState("");
  const [mediaError, setMediaError] = useState("");
  const [mediaLoading, setMediaLoading] = useState(true);
  const [retry, setRetry] = useState(0);
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [boxesVisible, setBoxesVisible] = useState(true);
  const detail = useQuery({
    queryKey: [
      "training-data-sample",
      owner.key,
      projectId,
      sample.version_id,
      sample.task_id,
      page,
      pageSize,
    ],
    queryFn: async ({ signal }) => {
      if (!ownsPreview(owner)) throw new Error("登录会话已变化，请重新打开冻结样本");
      const result = await trainingDatasetsApi.sample(
        projectId,
        sample.version_id,
        sample.task_id,
        page * pageSize,
        { signal },
        pageSize,
      );
      if (signal.aborted || !ownsPreview(owner))
        throw new DOMException("预览所属会话已变化", "AbortError");
      if (
        result.sample.version_id !== sample.version_id ||
        result.sample.task_id !== sample.task_id ||
        result.sample.sha256 !== sample.sha256 ||
        result.sample.label_sha256 !== sample.label_sha256
      ) {
        throw new Error("冻结标注响应身份与所选成员不一致");
      }
      return result;
    },
    enabled: Boolean(owner.userId && owner.token),
    retry: false,
  });
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let createdUrl = "";
    const owns = () => active && ownsPreview(owner);
    setMediaUrl("");
    setMediaError("");
    setMediaLoading(true);
    if (!owns()) {
      setMediaLoading(false);
      setMediaError("登录会话已变化，请重新打开冻结样本");
      return () => controller.abort();
    }
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
          observedRef.current();
        }
      });
    return () => {
      active = false;
      controller.abort();
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [owner, projectId, sample.version_id, sample.task_id, retry]);
  const value = detail.data;
  const lastPage = Math.max(0, Math.ceil((value?.total ?? 0) / pageSize) - 1);
  useEffect(() => {
    if (value && page > lastPage) setPage(lastPage);
  }, [value, page, lastPage]);
  const polygons = value?.labels.some((label) => label.type === "polygon") ?? false;
  const instanceMasks = value?.labels.filter(isInstanceMask) ?? [];
  const masks = instanceMasks.length > 0;
  return (
    <>
      <p className="mb-3 flex flex-wrap items-center gap-x-1 text-xs text-muted-foreground">
        样本 <CopyValueButton value={sample.task_id} kind="id" label="复制冻结样本任务 ID" /> ·{" "}
        {sample.split === "val" ? "验证集" : sample.split === "train" ? "训练集" : "划分未知"} ·{" "}
        {sample.width ?? "未知"} × {sample.height ?? "未知"}
      </p>
      <p className="mb-4 text-xs text-muted-foreground">
        本页读取训练版本保留的图片副本，不读取实时来源。图片经完整身份与字节校验后生成预览；实例来自对应的冻结标注。来源证据与副本当前可读性分别判断，预览按比例缩小。
      </p>
      {value && <TrainingDatasetSourceLineage lineage={value.source_lineage} />}
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
          <p>
            <CopyableError message={mediaError} />
          </p>
          <Button size="sm" className="mt-2" onClick={() => setRetry((value) => value + 1)}>
            重试冻结图片
          </Button>
        </div>
      )}
      {mediaUrl && masks && boxesVisible && !value?.sample.issue ? (
        <FrozenInstanceMaskPreview
          projectId={projectId}
          versionId={sample.version_id}
          taskId={sample.task_id}
          scopeKey={owner.key}
          sampleUrl={mediaUrl}
          labels={instanceMasks}
          assertOwner={() => {
            if (!ownsPreview(owner)) throw new DOMException("预览所属会话已变化", "AbortError");
          }}
        />
      ) : (
        mediaUrl && (
          <div className="relative mx-auto w-fit max-w-full overflow-hidden rounded-md border border-border bg-muted">
            <img
              src={mediaUrl}
              alt="冻结样本图片"
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
                  ) : !isInstanceMask(box) ? (
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
                  ) : null,
                )}
              </svg>
            )}
          </div>
        )
      )}
      {detail.isLoading && (
        <p role="status" className="mt-3 text-xs text-muted-foreground">
          正在读取冻结标注…
        </p>
      )}
      {detail.error && (
        <p role="alert" className="mt-3 text-sm text-status-danger">
          <CopyableError message={detail.error.message} />
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
              {masks ? "显示本页冻结实例掩码" : polygons ? "显示本页冻结多边形" : "显示本页冻结框"}
            </label>
            <span className="text-muted-foreground">
              当前显示 {page * pageSize + 1}–{page * pageSize + value.labels.length} / {value.total}{" "}
              个实例
            </span>
          </div>
          <div className={`${listStyles.surface} mt-2`}>
            <div className={listStyles.scroll}>
              <table className={listStyles.table}>
                <thead className="text-muted-foreground">
                  <tr>
                    <th className="py-2 font-medium">冻结类别</th>
                    <th className="py-2 font-medium">
                      {masks
                        ? "前景像素 / 像素边界（右下不含）"
                        : polygons
                          ? "顶点数量"
                          : "中心位置"}
                    </th>
                    <th className="py-2 font-medium">
                      {masks || polygons ? "面积占比 / 来源标注" : "宽 × 高（归一化）"}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {value.labels.map((box, index) => (
                    <tr key={index} className="border-t border-border">
                      <td className="py-2">{box.class_name}</td>
                      <td className="py-2 tabular-nums">
                        {isInstanceMask(box)
                          ? `${box.area_pixels} px · [${box.bbox_xyxy.join(", ")}]`
                          : box.type === "polygon"
                            ? `${box.points.length} 个顶点`
                            : `${box.x_center.toFixed(4)}, ${box.y_center.toFixed(4)}`}
                      </td>
                      <td className="py-2 tabular-nums">
                        {box.type === "polygon" || isInstanceMask(box) ? (
                          <span className="inline-flex items-center gap-1">
                            {(box.area * 100).toFixed(2)}% ·
                            <CopyValueButton
                              value={box.annotation_id}
                              kind="id"
                              label="复制冻结标注 ID"
                            />
                            · 标注版本 {box.annotation_version}
                            {isInstanceMask(box) && (
                              <CopyValueButton
                                value={box.pixel_sha256}
                                label="复制冻结掩码像素摘要"
                              />
                            )}
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
          </div>
        </>
      )}
      <ListPagination
        label="冻结标注"
        page={page}
        pageSize={pageSize}
        total={value?.total ?? undefined}
        count={value?.labels.length}
        loading={detail.isFetching}
        previousLabel="上一页标注"
        nextLabel="下一页标注"
        onPageChange={setPage}
        onPageSizeChange={(size) => {
          setPage(0);
          setPageSize(size);
        }}
      />
    </>
  );
}
