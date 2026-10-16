import { useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  trainingDatasetsApi,
  type DatasetSampleFilters,
  type TrainingDatasetSamplePage,
} from "@/api/trainingDatasets";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { TrainingDatasetSamplePanel } from "./TrainingDatasetSamplePreview";

type Props = {
  projectId: string;
  baselineVersionId: string;
  targetVersionId: string;
  contentSha256: string;
  onClose: () => void;
  onObserved: () => void;
};

type Owner = {
  userId: string;
  token: string;
  projectId: string;
  baselineVersionId: string;
  targetVersionId: string;
  contentSha256: string;
  key: string;
};

const MEMBER_PAGE_SIZE = 10;
const CONTENT_HASH = /^[a-f0-9]{64}$/;

function ownsComparison(owner: Owner) {
  const current = useAuthStore.getState();
  return (
    Boolean(owner.userId && owner.token) &&
    current.user?.id === owner.userId &&
    current.token === owner.token &&
    localStorage.getItem("token") === owner.token
  );
}

function useContentMembers(owner: Owner, versionId: string) {
  const [page, setPage] = useState(0);
  const [selectedTaskId, setSelectedTaskId] = useState("");
  const query = useQuery({
    queryKey: [
      "training-data-comparison-members",
      owner.key,
      owner.projectId,
      versionId,
      owner.contentSha256,
      page,
    ],
    queryFn: async ({ signal }) => {
      if (!ownsComparison(owner)) throw new Error("登录会话已变化，请重新打开样本对照");
      const filters: DatasetSampleFilters & { content_sha256: string } = {
        limit: MEMBER_PAGE_SIZE,
        offset: page * MEMBER_PAGE_SIZE,
        content_sha256: owner.contentSha256,
      };
      const result = await trainingDatasetsApi.samples(owner.projectId, versionId, filters, {
        signal,
      });
      if (signal.aborted || !ownsComparison(owner))
        throw new DOMException("样本对照所属会话已变化", "AbortError");
      if (
        result.source_version_id !== versionId ||
        result.offset !== page * MEMBER_PAGE_SIZE ||
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
  const selected =
    query.data?.items.find((sample) => sample.task_id === selectedTaskId) ?? query.data?.items[0];
  return {
    query,
    page,
    selected,
    select: setSelectedTaskId,
    navigate: (next: number) => {
      setSelectedTaskId("");
      setPage(next);
    },
  };
}

export function TrainingDatasetSampleComparison(props: Props) {
  const userId = useAuthStore((state) => state.user?.id ?? "");
  const token = useAuthStore((state) => state.token ?? "");
  const returnFocusRef = useRef<HTMLElement | null>(
    document.activeElement instanceof HTMLElement ? document.activeElement : null,
  );
  const owner = useMemo(
    () => ({
      userId,
      token,
      projectId: props.projectId,
      baselineVersionId: props.baselineVersionId,
      targetVersionId: props.targetVersionId,
      contentSha256: props.contentSha256,
      key: crypto.randomUUID(),
    }),
    [
      userId,
      token,
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
      <ComparisonContent key={owner.key} owner={owner} onObserved={props.onObserved} />
      <div className="mt-4 flex justify-end border-t border-border pt-3">
        <Button size="sm" onClick={props.onClose}>
          关闭对照
        </Button>
      </div>
    </Modal>
  );
}

function ComparisonContent({ owner, onObserved }: { owner: Owner; onObserved: () => void }) {
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
      <p className="mb-4 break-all font-mono text-xs text-muted-foreground">
        内容 SHA256 · {owner.contentSha256}
      </p>
      <div className="grid min-w-0 gap-4 lg:grid-cols-2">
        <ComparisonSide
          label="基线版本"
          versionId={owner.baselineVersionId}
          owner={owner}
          members={baseline}
          opposite={target.query.data}
          onObserved={onObserved}
        />
        <ComparisonSide
          label="当前版本"
          versionId={owner.targetVersionId}
          owner={owner}
          members={target}
          opposite={baseline.query.data}
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
  onObserved,
}: {
  label: "基线版本" | "当前版本";
  versionId: string;
  owner: Owner;
  members: ReturnType<typeof useContentMembers>;
  opposite: TrainingDatasetSamplePage | undefined;
  onObserved: () => void;
}) {
  const { query, page, selected, select, navigate } = members;
  const data = query.data;
  return (
    <section aria-label={label} className="min-w-0 rounded-lg border border-border p-3">
      <header className="mb-3 flex flex-wrap items-start justify-between gap-2 border-b border-border pb-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-foreground">{label}</h3>
          <p className="mt-1 break-all text-xs text-muted-foreground" title={versionId}>
            版本 {versionId}
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
          <p>{query.error.message}</p>
          <Button size="sm" className="mt-2" onClick={() => void query.refetch()}>
            重试成员读取
          </Button>
        </div>
      )}
      {data?.total === 0 && (
        <p className="py-8 text-center text-sm text-muted-foreground">
          {opposite?.total === 0
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
            <label className="grid w-full min-w-0 gap-1.5 text-xs text-muted-foreground">
              选择冻结成员
              <select
                aria-label={`${label}冻结成员`}
                className="h-8 w-full min-w-0 rounded-md border border-border bg-background px-2 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
                value={selected?.task_id ?? ""}
                disabled={query.isFetching || !data.items.length}
                onChange={(event) => select(event.target.value)}
              >
                {data.items.map((sample) => (
                  <option key={sample.task_id} value={sample.task_id}>
                    {sample.task_id} ·{" "}
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
            <span className="text-xs tabular-nums text-muted-foreground">
              {data.items.length
                ? `${page * MEMBER_PAGE_SIZE + 1}–${page * MEMBER_PAGE_SIZE + data.items.length}`
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
          {data.total > MEMBER_PAGE_SIZE && (
            <div className="mt-3 flex justify-end gap-2">
              <Button
                size="xs"
                disabled={!page || query.isFetching}
                onClick={() => navigate(page - 1)}
              >
                上一页成员
              </Button>
              <Button
                size="xs"
                disabled={(page + 1) * MEMBER_PAGE_SIZE >= data.total || query.isFetching}
                onClick={() => navigate(page + 1)}
              >
                下一页成员
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
