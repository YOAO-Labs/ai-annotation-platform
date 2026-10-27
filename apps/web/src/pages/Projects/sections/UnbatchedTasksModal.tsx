import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { Modal } from "@/components/ui/Modal";
import { Thumbnail } from "@/components/Thumbnail";
import { useTaskList } from "@/hooks/useTasks";
import { useListPageState } from "@/hooks/useListPageState";
import { useInfiniteListPagination } from "@/hooks/useInfiniteListPagination";

export function UnbatchedTasksModal({
  projectId,
  count,
  onClose,
}: {
  projectId: string;
  count: number;
  onClose: () => void;
}) {
  const state = useListPageState(projectId);
  const query = useTaskList(projectId, { unbatched: true, limit: state.pageSize });
  const pagination = useInfiniteListPagination(projectId, query, state);
  return (
    <Modal open onClose={onClose} title={`未归类任务（${count}）`} width={560}>
      <div className={listStyles.surface}>
        {query.isLoading && <div className={listStyles.empty}>加载中…</div>}
        {query.isError && (
          <div role="alert" className={listStyles.empty}>
            任务加载失败，请重新打开后重试。
          </div>
        )}
        {!query.isLoading && !pagination.items.length && (
          <div className={listStyles.empty}>没有未归类任务。</div>
        )}
        <div className="max-h-[60vh] overflow-y-auto">
          {pagination.items.map((task) => (
            <div key={task.id} className={`${listStyles.row} flex items-center gap-2.5`}>
              <Thumbnail
                src={task.thumbnail_url}
                blurhash={task.blurhash}
                alt={task.file_name}
                width={40}
                height={40}
              />
              <div className="flex min-w-0 flex-col gap-0.5">
                <div className="truncate text-sm text-foreground" title={task.file_name}>
                  {task.file_name}
                </div>
                <div className="text-xs tabular-nums text-muted-foreground">{task.display_id}</div>
              </div>
            </div>
          ))}
        </div>
        <ListPagination
          label="未归类任务"
          page={pagination.page}
          pageSize={pagination.pageSize}
          total={pagination.total ?? count}
          hasNext={pagination.hasNext}
          loading={query.isFetchingNextPage || query.isLoading}
          onPageChange={pagination.onPageChange}
          onPageSizeChange={pagination.setPageSize}
        />
      </div>
    </Modal>
  );
}
