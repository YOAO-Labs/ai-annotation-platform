import { useId, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { usersApi } from "@/api/users";
import { Button } from "@/components/ui/Button";
import { verifiedContentError } from "@/hooks/useVerifiedImage";

const field = "rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground";

export function StorageLocationAccessFields({
  session,
  scope,
  userIds,
  disabled,
  onScope,
  onUsers,
}: {
  session: string | number | null;
  scope: "restricted" | "global";
  userIds: string[];
  disabled: boolean;
  onScope: (scope: "restricted" | "global") => void;
  onUsers: (ids: string[]) => void;
}) {
  const scopeHint = useId();
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const users = useQuery({
    queryKey: ["storage-grant-users", session, search, page],
    queryFn: ({ signal }) =>
      usersApi.page(
        { role: "project_admin", status: "active", search, page, page_size: 10 },
        signal,
      ),
  });
  return (
    <fieldset
      disabled={disabled}
      className="mt-3 min-w-0 rounded-md border border-border p-3 text-sm"
    >
      <legend className="px-1 font-medium">位置使用范围</legend>
      <label className="grid gap-1">
        使用范围
        <select
          aria-label="位置使用范围"
          aria-describedby={scopeHint}
          className={field}
          value={scope}
          onChange={(e) => onScope(e.target.value as "restricted" | "global")}
        >
          <option value="restricted">仅指定的数据管理员</option>
          <option value="global">所有数据管理员</option>
        </select>
      </label>
      <p id={scopeHint} className="mt-2 text-xs leading-relaxed text-muted-foreground">
        超级管理员始终可用。此授权允许项目管理员为自己创建的数据集接入该位置，不授予其他数据集的管理权。标注员通过分配任务读取图片。
      </p>
      {scope === "restricted" && (
        <>
          <label className="mt-2 grid gap-1">
            搜索可授权的数据管理员
            <input
              className={field}
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
              placeholder="姓名或邮箱"
            />
          </label>
          <div className="my-2 flex items-center gap-2">
            <span>已授权 {userIds.length} 人，可搜索后取消勾选</span>
            <Button
              size="sm"
              disabled={disabled || userIds.length === 0}
              onClick={() => onUsers([])}
            >
              清空人员授权
            </Button>
          </div>
          {users.isPending ? (
            <p role="status">正在加载人员…</p>
          ) : (
            <div className="grid gap-2 sm:grid-cols-2">
              {users.data?.items.map((user) => (
                <label key={user.id} className="flex min-w-0 items-start gap-2">
                  <input
                    type="checkbox"
                    checked={userIds.includes(user.id)}
                    onChange={(e) =>
                      onUsers(
                        e.target.checked
                          ? [...userIds, user.id]
                          : userIds.filter((id) => id !== user.id),
                      )
                    }
                  />
                  <span className="min-w-0 break-words">
                    {user.name} · {user.email}
                  </span>
                </label>
              ))}
              {users.data?.items.length === 0 && <p>没有匹配的数据管理员</p>}
            </div>
          )}
          <div className="mt-2 flex items-center gap-2">
            <Button
              size="sm"
              disabled={disabled || users.isPending || page <= 1}
              onClick={() => setPage((p) => p - 1)}
            >
              上一页人员
            </Button>
            <span>
              第 {page} 页 · 共 {users.data?.total ?? 0} 人
            </span>
            <Button
              size="sm"
              disabled={disabled || users.isPending || page >= (users.data?.pages ?? 0)}
              onClick={() => setPage((p) => p + 1)}
            >
              下一页人员
            </Button>
          </div>
          {users.error && (
            <p role="alert" className="text-status-danger">
              {verifiedContentError(users.error)}
            </p>
          )}
        </>
      )}
    </fieldset>
  );
}
