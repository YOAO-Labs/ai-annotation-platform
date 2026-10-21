import { ListPagination } from "@/components/ui/ListPagination";

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
  return (
    <ListPagination
      label="项目"
      page={page - 1}
      pageSize={pageSize}
      total={total}
      loading={loading}
      onPageChange={(next) => onPageChange(next + 1)}
      onPageSizeChange={onPageSizeChange}
      pageSizeLabel="每页项目数"
    />
  );
}
