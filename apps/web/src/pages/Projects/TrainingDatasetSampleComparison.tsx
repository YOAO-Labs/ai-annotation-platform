import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { randomId } from "@/utils/id";
import { CopyableError } from "@/components/ui/CopyableError";
import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  trainingDatasetsApi,
  type DatasetSampleFilters,
  type TrainingDatasetSamplePage,
} from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { getAuthSessionRevision } from "@/stores/authSession";
import { Button } from "@/components/ui/Button";
import { CopyValueButton } from "@/components/ui/CopyValueButton";
import { Modal } from "@/components/ui/Modal";
import { TrainingDatasetSamplePanel } from "./TrainingDatasetSamplePreview";
import { useTrainingSessionRevision } from "../Training/trainingViewOwner";

type Props = {
  projectId: string;
  baselineVersionId: string;
  targetVersionId: string;
  contentSha256: string;
  baselineUnknownContents?: number | null;
  targetUnknownContents?: number | null;
  onClose: () => void;
  onObserved: () => void;
};

type Owner = {
  userId: string;
  token: string;
  revision: number;
  projectId: string;
  baselineVersionId: string;
  targetVersionId: string;
  contentSha256: string;
  key: string;
};

const CONTENT_HASH = /^[a-f0-9]{64}$/;

function ownsComparison(owner: Owner) {
  const current = useAuthStore.getState();
  return (
    getAuthSessionRevision() === owner.revision &&
    Boolean(owner.userId && owner.token) &&
    current.user?.id === owner.userId &&
    current.token === owner.token &&
    localStorage.getItem("token") === owner.token
  );
}

function useContentMembers(owner: Owner, versionId: string) {
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const query = useQuery({
    queryKey: [
      "training-data-comparison-members",
      owner.key,
      owner.projectId,
      versionId,
      owner.contentSha256,
      page,
      pageSize,
    ],
    queryFn: async ({ signal }) => {
      if (!ownsComparison(owner)) throw new Error("登录会话已变化，请重新打开样本对照");
      const filters: DatasetSampleFilters & { content_sha256: string } = {
        limit: pageSize,
        offset: page * pageSize,
        content_sha256: owner.contentSha256,
      };
      const result = await trainingDatasetsApi.samples(owner.projectId, versionId, filters, {
        signal,
      });
      if (signal.aborted || !ownsComparison(owner))
        throw new DOMException("样本对照所属会话已变化", "AbortError");
      if (
        result.source_version_id !== versionId ||
        result.offset !== page * pageSize ||
        result.items.some(
          (sample) =>
            sample.version_id !== versionId ||
            sample.sha256 !== owner.contentSha256 ||
            !sample.included,
        )
      ) {
        throw new Error("冻结成员响应与所选版本或内容身份不一致");
      }
      return result;
    },
    enabled: CONTENT_HASH.test(owner.contentSha256) && ownsComparison(owner),
    retry: false,
  });
  const lastPage = Math.max(0, Math.ceil((query.data?.total ?? 0) / pageSize) - 1);
  useEffect(() => {
    if (query.data && page > lastPage) {
      setSelectedTaskId("");
      setPage(lastPage);
    }
  }, [query.data, page, lastPage]);
  const selected =
    query.data?.items.find((sample) => sample.task_id === selectedTaskId) ?? query.data?.items[0];
  return {
    query,
    page,
    pageSize,
    resize: (size: number) => {
      setSelectedTaskId("");
      setPage(0);
      setPageSize(size);
    },
    selected,
    select: setSelectedTaskId,
    navigate: (next: number) => {
      setSelectedTaskId("");
      setPage(next);
    },
  };
}

export function TrainingDatasetSampleComparison(props: Props) {
  const revision = useTrainingSessionRevision();
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const returnFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  const owner = useMemo(
    () => ({
      userId,
      token,
      revision,
      projectId: props.projectId,
      baselineVersionId: props.baselineVersionId,
      targetVersionId: props.targetVersionId,
      contentSha256: props.contentSha256,
      key: randomId(),
    }),
    [
      userId,
      token,
      revision,
      props.projectId,
      props.baselineVersionId,
      props.targetVersionId,
      props.contentSha256,
    ],
  );
  return (
    <Modal
      open
      onClose={props.onClose}
      title="冻结样本前后对照"
      width={1320}
      stopEscapePropagation
      returnFocusRef={returnFocusRef}
    >
      <ComparisonContent
        key={owner.key}
        owner={owner}
        onObserved={props.onObserved}
        baselineUnknownContents={props.baselineUnknownContents ?? null}
        targetUnknownContents={props.targetUnknownContents ?? null}
      />
      <div className="mt-4 flex justify-end border-t border-border pt-3">
        <Button size="sm" onClick={props.onClose}>
          关闭对照
        </Button>
      </div>
    </Modal>
  );
}

function ComparisonContent({
  owner,
  onObserved,
  baselineUnknownContents,
  targetUnknownContents,
}: {
  owner: Owner;
  onObserved: () => void;
  baselineUnknownContents: number | null;
  targetUnknownContents: number | null;
}) {
  const baseline = useContentMembers(owner, owner.baselineVersionId);
  const target = useContentMembers(owner, owner.targetVersionId);
  if (!CONTENT_HASH.test(owner.contentSha256))
    return (
      <p role="alert" className="text-sm text-status-danger">
        完整内容 SHA 不可用，无法解析冻结成员。
      </p>
    );
  if (!ownsComparison(owner))
    return (
      <p role="alert" className="text-sm text-status-caution">
        登录会话已变化，请关闭对照后重试。
      </p>
    );
  return (
    <>
      <p className="mb-2 text-xs text-muted-foreground">
        按相同图片内容读取两侧训练成员。类别编号按各自版本解释，标注来自各自冻结标签。
      </p>
      <p className="mb-4 flex items-center gap-1 text-xs text-muted-foreground">
        内容 SHA256 ·
        <CopyValueButton value={owner.contentSha256} label="复制样本内容 SHA256" />
      </p>
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <ComparisonSide
          label="基线版本"
          versionId={owner.baselineVersionId}
          owner={owner}
          members={baseline}
          opposite={target.query.data}
          unknownContents={baselineUnknownContents}
          onObserved={onObserved}
        />
        <ComparisonSide
          label="当前版本"
          versionId={owner.targetVersionId}
          owner={owner}
          members={target}
          opposite={baseline.query.data}
          unknownContents={targetUnknownContents}
          onObserved={onObserved}
        />
      </div>
    </>
  );
}

function ComparisonSide({
  label,
  versionId,
  owner,
  members,
  opposite,
  unknownContents,
  onObserved,
}: {
  label: "基线版本" | "当前版本";
  versionId: string;
  owner: Owner;
  members: ReturnType<typeof useContentMembers>;
  opposite: TrainingDatasetSamplePage | undefined;
  unknownContents: number | null;
  onObserved: () => void;
}) {
  const { query, page, pageSize, resize, selected, select, navigate } = members;
  const data = query.data;
  return (
    <section aria-label={label} className={`${listStyles.surface} min-w-0 p-3`}>
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2 border-b border-border pb-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-foreground">{label}</h3>
          <p className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
            版本 <CopyValueButton value={versionId} kind="id" label={`复制${label}数据版本 ID`} />
          </p>
        </div>
        {data && (
          <p className="text-xs tabular-nums text-muted-foreground">共 {data.total} 个训练成员</p>
        )}
      </header>
      {query.isLoading && (
        <p role="status" className="py-8 text-center text-sm text-muted-foreground">
          正在查找冻结成员…
        </p>
      )}
      {query.error && (
        <div
          role="alert"
          className="rounded-md bg-status-caution-soft p-3 text-sm text-status-caution"
        >
          <p>
            <CopyableError message={query.error.message} />
          </p>
          <Button size="sm" className="mt-2" onClick={() => void query.refetch()}>
            重试成员读取
          </Button>
        </div>
      )}
      {data?.total === 0 && (
        <p className="py-8 text-center text-sm text-muted-foreground">
          {unknownContents !== 0
            ? `该版本已知身份中未找到匹配；内容身份未知 ${unknownContents ?? "待确认"} 张，新增或移除尚不能完全确认。`
            : opposite?.total === 0
              ? "两侧都没有该内容的训练成员。"
              : opposite && opposite.total > 0
                ? label === "基线版本"
                  ? "新增内容：该内容未出现在基线版本中。"
                  : "移除内容：该内容未出现在当前版本中。"
                : "该版本中没有该内容的训练成员；另一侧尚未确认。"}
        </p>
      )}
      {data && data.total > 0 && (
        <>
          <div className="mb-3 flex flex-wrap items-end justify-between gap-2">
            <div className="flex w-full min-w-0 items-end gap-2">
              <label className="grid min-w-0 flex-1 gap-1.5 text-xs text-muted-foreground">
                选择冻结成员
                <select
                  aria-label={`${label}冻结成员`}
                  className="h-8 w-full min-w-0 rounded-md border border-border bg-background px-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  value={selected?.task_id ?? ""}
                  disabled={query.isFetching || !data.items.length}
                  onChange={(event) => select(event.target.value)}
                >
                  {data.items.map((sample, index) => (
                    <option key={sample.task_id} value={sample.task_id}>
                      样本 {page * pageSize + index + 1} ·{" "}
                      {sample.split === "val"
                        ? "验证集"
                        : sample.split === "train"
                          ? "训练集"
                          : "划分未知"}{" "}
                      · {sample.instances == null ? "实例未知" : `${sample.instances} 个实例`}
                    </option>
                  ))}
                </select>
              </label>
              <CopyValueButton
                value={selected?.task_id}
                kind="id"
                label={`复制${label}冻结成员任务 ID`}
              />
            </div>
            <span className="text-xs tabular-nums text-muted-foreground">
              {data.items.length
                ? `${page * pageSize + 1}–${page * pageSize + data.items.length}`
                : "当前页为空"}{" "}
              / {data.total} 个成员
            </span>
          </div>
          {selected && (
            <TrainingDatasetSamplePanel
              projectId={owner.projectId}
              sample={selected}
              onObserved={onObserved}
            />
          )}
          {!selected && (
            <p className="text-sm text-muted-foreground">当前成员页为空，请返回上一页。</p>
          )}
        </>
      )}
      <ListPagination
        label={`${label}成员`}
        page={page}
        pageSize={pageSize}
        total={data?.total}
        count={data?.items.length}
        loading={query.isFetching}
        previousLabel="上一页成员"
        nextLabel="下一页成员"
        onPageChange={navigate}
        onPageSizeChange={resize}
      />
    </section>
  );
}
