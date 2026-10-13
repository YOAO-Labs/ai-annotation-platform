import { Button } from "@/components/ui/Button";
import { PROJECT_PAGE_SIZES } from "./dashboardUrlState";

interface Props {
  page: number;
  pageSize: number;
  total: number;
  loading: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
}

export function ProjectPagination({
  page,
  pageSize,
  total,
  loading,
  onPageChange,
  onPageSizeChange,
}: Props) {
  const pages = Math.max(1, Math.ceil(total / pageSize));
  return (
    <nav
      aria-label="项目分页"
      className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-4 py-3 text-xs text-muted-foreground"
    >
      <span role="status">
        {loading ? "正在加载项目…" : `共 ${total} 个项目 · 第 ${page} / ${pages} 页`}
      </span>
      <div className="flex flex-wrap items-center gap-2">
        <label className="mr-1 flex items-center gap-1.5">
          每页
          <select
            aria-label="每页项目数"
            value={pageSize}
            onChange={(event) => onPageSizeChange(Number(event.target.value))}
            disabled={loading}
            className="h-6 rounded-sm border border-border bg-background px-1 text-xs text-foreground"
          >
            {PROJECT_PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
          个
        </label>
        <Button size="xs" disabled={loading || page <= 1} onClick={() => onPageChange(page - 1)}>
          上一页
        </Button>
        <Button
          size="xs"
          disabled={loading || page >= pages}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
        </Button>
      </div>
    </nav>
  );
}
