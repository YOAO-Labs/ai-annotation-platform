import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/utils";

export const LIST_PAGE_SIZES = [10, 20, 50, 100] as const;
export const DEFAULT_LIST_PAGE_SIZE = 20;

export function listPageSize(value: unknown, fallback = DEFAULT_LIST_PAGE_SIZE) {
  const size = Number(value);
  return (LIST_PAGE_SIZES as readonly number[]).includes(size) ? size : fallback;
}

export interface ListPaginationProps {
  label: string;
  /** Zero-based page index; cursor owners keep their own cursor history. */
  page: number;
  pageSize: number;
  total?: number;
  count?: number;
  hasNext?: boolean;
  loading?: boolean;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
  previousLabel?: string;
  nextLabel?: string;
  pageSizeLabel?: string;
  className?: string;
}

export function ListPagination({
  label,
  page,
  pageSize,
  total,
  count,
  hasNext,
  loading = false,
  onPageChange,
  onPageSizeChange,
  previousLabel,
  nextLabel,
  pageSizeLabel,
  className,
}: ListPaginationProps) {
  const pages = total == null ? undefined : Math.max(1, Math.ceil(total / pageSize));
  const showTotal = total != null && !(loading && total === 0);
  const canNext = hasNext ?? (pages != null && page + 1 < pages);
  const sizes = Array.from(new Set<number>([...LIST_PAGE_SIZES, pageSize])).sort((a, b) => a - b);
  return (
    <nav
      aria-label={`${label}分页`}
      aria-busy={loading}
      className={cn(
        "flex flex-wrap items-center justify-between gap-x-4 gap-y-3 border-t border-border px-3 py-3 text-xs text-muted-foreground sm:px-4",
        className,
      )}
    >
      <span role="status" className="tabular-nums">
        {showTotal ? `共 ${total} 条 · 第 ${page + 1} / ${pages} 页` : `第 ${page + 1} 页`}
        {total == null && count != null && ` · 本页 ${count} 条`}
        {loading && <span className="ml-2">正在加载…</span>}
      </span>
      <div className="flex flex-wrap items-center gap-2">
        <label className="mr-1 flex items-center gap-1.5 whitespace-nowrap">
          每页
          <select
            aria-label={pageSizeLabel ?? `每页${label}数`}
            value={pageSize}
            disabled={loading}
            onChange={(event) => onPageSizeChange(Number(event.target.value))}
            className="h-7 min-w-14 rounded-md border border-border bg-background px-2 text-xs tabular-nums text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            {sizes.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </select>
          行
        </label>
        <Button
          type="button"
          size="xs"
          aria-label={previousLabel}
          disabled={loading || page <= 0}
          onClick={() => onPageChange(page - 1)}
        >
          上一页
        </Button>
        <Button
          type="button"
          size="xs"
          aria-label={nextLabel}
          disabled={loading || !canNext}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
        </Button>
      </div>
    </nav>
  );
}
