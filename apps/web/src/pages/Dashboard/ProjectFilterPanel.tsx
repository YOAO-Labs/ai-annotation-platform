import { useEffect, useState } from "react";

import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";
import { FilterSection, FilterToggle } from "@/components/filters/FilterControls";
import { FilterPanel } from "@/components/filters/FilterPanel";
import { FilterTrigger } from "@/components/filters/FilterTrigger";
import { Input } from "@/components/shadcn/ui/input";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { useUsers } from "@/hooks/useUsers";
import { useAuthStore } from "@/stores/authStore";
import { PROJECT_DATA_TYPES } from "@/constants/toolUnits";
import { ROLE_LABELS } from "@/constants/roles";
import { cn } from "@/lib/utils";
import {
  EMPTY_FILTERS,
  type DashboardDataType,
  type DashboardFilters,
  type DashboardStatus,
} from "./dashboardUrlState";

const STATUS_OPTIONS: { value: DashboardStatus | ""; label: string }[] = [
  { value: "", label: "全部" },
  { value: "in_progress", label: "进行中" },
  { value: "pending_review", label: "待审核" },
  { value: "completed", label: "已完成" },
];

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initial: DashboardFilters;
  onApply: (next: DashboardFilters) => void;
  count: number;
};

export function ProjectFilterPanel({ open, onOpenChange, initial, onApply, count }: Props) {
  const [draft, setDraft] = useState(initial);
  const [memberQuery, setMemberQuery] = useState("");
  const currentUser = useAuthStore((state) => state.user);
  const usersQuery = useUsers();
  const users = usersQuery.data ?? [];
  const query = memberQuery.trim().toLocaleLowerCase();
  const matchingUsers = users.filter((user) =>
    `${user.name} ${user.email ?? ""}`.toLocaleLowerCase().includes(query),
  );
  const selectedMember = users.find((user) => user.id === draft.member_id);
  const invalidDateRange = Boolean(
    draft.created_from && draft.created_to && draft.created_from > draft.created_to,
  );

  useEffect(() => {
    if (open) {
      setDraft(initial);
      setMemberQuery("");
    }
  }, [open, initial]);

  const toggleType = (key: DashboardDataType) =>
    setDraft((previous) => ({
      ...previous,
      data_type: previous.data_type.includes(key)
        ? previous.data_type.filter((type) => type !== key)
        : [...previous.data_type, key],
    }));

  return (
    <FilterPanel
      open={open}
      onOpenChange={onOpenChange}
      trigger={<FilterTrigger size="md" count={count} countLabel="附加筛选条件（不含状态标签）" />}
      title="高级筛选"
      description="组合条件，点击应用后更新项目列表。"
      className="w-[28rem]"
      footer={
        <div className="flex items-center justify-between gap-2">
          <Button size="sm" variant="ghost" onClick={() => setDraft(EMPTY_FILTERS)}>
            重置
          </Button>
          <div className="flex gap-2">
            <Button size="sm" onClick={() => onOpenChange(false)}>
              取消
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={invalidDateRange}
              onClick={() => {
                if (invalidDateRange) return;
                onApply(draft);
                onOpenChange(false);
              }}
            >
              应用
            </Button>
          </div>
        </div>
      }
    >
      <div className="flex flex-col gap-4">
        <FilterSection title="状态">
          <div className="flex flex-wrap gap-1.5">
            {STATUS_OPTIONS.map((status) => (
              <FilterToggle
                key={status.value}
                active={(draft.status ?? "") === status.value}
                onClick={() => setDraft({ ...draft, status: status.value || undefined })}
              >
                {status.label}
              </FilterToggle>
            ))}
          </div>
        </FilterSection>
        <FilterSection title="数据类型">
          <div className="flex flex-wrap gap-2">
            {PROJECT_DATA_TYPES.map((type) => (
              <label
                key={type.id}
                title={type.hint}
                className={cn(
                  "inline-flex cursor-pointer items-center gap-2 rounded-md border border-border px-2.5 py-1.5 text-xs",
                  draft.data_type.includes(type.id) && "border-brand/20 bg-brand/10 text-brand",
                )}
              >
                <input
                  type="checkbox"
                  checked={draft.data_type.includes(type.id)}
                  onChange={() => toggleType(type.id)}
                  className="size-3 accent-brand"
                />
                {type.label}
              </label>
            ))}
          </div>
        </FilterSection>
        <FilterSection title="成员">
          <div className="flex flex-wrap items-center gap-1.5">
            <FilterToggle
              active={Boolean(currentUser && draft.member_id === currentUser.id)}
              disabled={!currentUser}
              onClick={() => setDraft({ ...draft, member_id: currentUser?.id })}
            >
              我参与的
            </FilterToggle>
            <FilterToggle
              active={!draft.member_id}
              onClick={() => setDraft({ ...draft, member_id: undefined })}
            >
              不限
            </FilterToggle>
            {draft.member_id && (
              <ActiveFilterChip
                label="已选成员"
                value={selectedMember?.name ?? "指定成员"}
                onRemove={() => setDraft({ ...draft, member_id: undefined })}
              />
            )}
          </div>
          <Input
            aria-label="搜索成员"
            placeholder="搜索成员姓名或邮箱"
            value={memberQuery}
            onChange={(event) => setMemberQuery(event.target.value)}
            className="h-8"
          />
          <div className="max-h-36 overflow-y-auto rounded-md border border-border">
            {usersQuery.isError ? (
              <div
                role="alert"
                className="flex items-center justify-between gap-2 p-3 text-xs text-status-danger"
              >
                成员加载失败，已选条件仍保留。
                <Button size="xs" onClick={() => void usersQuery.refetch()}>
                  重试
                </Button>
              </div>
            ) : usersQuery.isLoading ? (
              <div role="status" className="p-3 text-xs text-muted-foreground">
                加载成员…
              </div>
            ) : matchingUsers.length ? (
              matchingUsers.map((user) => (
                <label
                  key={user.id}
                  className={cn(
                    "flex cursor-pointer items-center gap-2 border-b border-border px-2.5 py-2 text-sm last:border-b-0 hover:bg-accent",
                    draft.member_id === user.id && "bg-brand/5",
                  )}
                >
                  <input
                    type="radio"
                    name="project-filter-member"
                    checked={draft.member_id === user.id}
                    onChange={() => setDraft({ ...draft, member_id: user.id })}
                    className="size-3 accent-brand"
                  />
                  <UserAvatar user={user} size="sm" />
                  <span className="min-w-0 flex-1 truncate">{user.name}</span>
                  <span className="text-2xs text-muted-foreground">
                    {ROLE_LABELS[user.role as keyof typeof ROLE_LABELS] ?? user.role}
                  </span>
                </label>
              ))
            ) : (
              <div className="p-3 text-center text-xs text-muted-foreground">
                {query ? "没有匹配成员" : "暂无成员"}
              </div>
            )}
          </div>
        </FilterSection>
        <FilterSection title="创建时间">
          <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-center gap-2">
            <Input
              type="date"
              aria-label="创建开始日期"
              value={draft.created_from ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, created_from: event.target.value || undefined })
              }
              className="h-8 min-w-0 text-xs"
            />
            <span className="text-xs text-muted-foreground">至</span>
            <Input
              type="date"
              aria-label="创建结束日期"
              value={draft.created_to ?? ""}
              onChange={(event) =>
                setDraft({ ...draft, created_to: event.target.value || undefined })
              }
              className="h-8 min-w-0 text-xs"
            />
          </div>
          {invalidDateRange && (
            <p role="alert" className="text-xs text-status-danger">
              开始日期不能晚于结束日期
            </p>
          )}
        </FilterSection>
      </div>
    </FilterPanel>
  );
}
