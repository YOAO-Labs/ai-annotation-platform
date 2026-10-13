import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useTransferProject } from "@/hooks/useProjects";
import { usersApi } from "@/api/users";
import type { ProjectResponse } from "@/api/projects";

// 底色按 active/idle 互斥下发：基础类不带 bg-*，否则模板字符串无 tailwind-merge，
// 无条件 bg-transparent 会盖掉 active 的 bg-brand/10 (源顺序裁决)。
const CANDIDATE_ITEM_BASE =
  "flex w-full cursor-pointer appearance-none items-center gap-2.5 border-0 border-b border-border px-3 py-2.5 text-left text-foreground disabled:cursor-not-allowed disabled:opacity-50";

export function OwnerSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const [modalOpen, setModalOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const transfer = useTransferProject(project.id);

  const { data: candidates = [] } = useQuery({
    queryKey: ["users", "project_admin"],
    queryFn: () => usersApi.list({ role: "project_admin" }),
    enabled: modalOpen,
  });

  const onConfirm = () => {
    if (!selected) return;
    transfer.mutate(selected, {
      onSuccess: () => {
        pushToast({ msg: "负责人已转移", kind: "success" });
        setModalOpen(false);
        setSelected(null);
      },
      onError: (err) => pushToast({ msg: "转移失败", sub: (err as Error).message }),
    });
  };

  return (
    <>
      <Card>
        <div className="border-b border-border px-4 py-3.5">
          <h3 className="text-sm font-semibold">项目负责人</h3>
        </div>
        <div className="flex flex-col gap-3.5 p-4">
          <div className="flex items-center gap-3">
            <UserAvatar
              size="md"
              user={{ name: project.owner_name, avatar_ref: project.owner_avatar_ref }}
            />
            <div>
              <div className="text-sm font-medium">{project.owner_name ?? "—"}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">
                当前负责人 · 拥有此项目的全部管理权
              </div>
            </div>
          </div>
          <div className="rounded-md border border-border bg-muted px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">
            <strong className="text-foreground">转移规则：</strong>{" "}
            负责人转移操作仅由超级管理员执行。新负责人必须是项目管理员（project_admin）角色。转移后原负责人将失去对此项目的可见性，除非被指派为成员。
          </div>
          <div>
            <Button variant="primary" onClick={() => setModalOpen(true)}>
              转移负责人
            </Button>
          </div>
        </div>
      </Card>

      <Modal
        open={modalOpen}
        onClose={() => setModalOpen(false)}
        title="转移项目负责人"
        width={520}
      >
        <div className="flex flex-col gap-3">
          <div className="text-xs text-muted-foreground">
            选择新的项目负责人（仅 project_admin 可作为目标）
          </div>
          <div className="max-h-80 overflow-y-auto rounded-md border border-border">
            {candidates.length === 0 && (
              <div className="p-6 text-center text-sm text-muted-foreground">
                暂无可选 project_admin
              </div>
            )}
            {candidates.map((u) => {
              const active = selected === u.id;
              const isCurrent = u.id === project.owner_id;
              return (
                <button
                  key={u.id}
                  type="button"
                  disabled={isCurrent}
                  onClick={() => setSelected(u.id)}
                  className={`${CANDIDATE_ITEM_BASE} ${active ? "bg-brand/10" : "bg-transparent"}`}
                >
                  <UserAvatar user={u} size="sm" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">{u.name}</div>
                    <div className="text-xs text-muted-foreground">
                      {u.email}
                      {isCurrent ? " · 当前负责人" : ""}
                    </div>
                  </div>
                </button>
              );
            })}
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setModalOpen(false)}>
              取消
            </Button>
            <Button
              variant="primary"
              disabled={!selected || transfer.isPending}
              onClick={onConfirm}
            >
              {transfer.isPending ? "转移中..." : "确认转移"}
            </Button>
          </div>
        </div>
      </Modal>
    </>
  );
}
