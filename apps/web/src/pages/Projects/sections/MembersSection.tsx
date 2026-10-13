import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useProjectMembers, useRemoveProjectMember } from "@/hooks/useProjects";
import { AssignMemberModal } from "@/components/projects/AssignMemberModal";
import { MemberRoleChangeModal } from "@/components/projects/MemberRoleChangeModal";
import { PROJECT_ROLE_LABELS, ROLE_LABELS } from "@/constants/roles";
import type { ProjectResponse, ProjectMemberResponse } from "@/api/projects";
import type { PlatformRole, ProjectRole } from "@/types";

const PLACEHOLDER_CLASS = "p-8 text-center text-sm text-muted-foreground";
const HEAD_CELL_BASE =
  "border-b border-border bg-muted px-3 py-2.5 text-left text-xs font-medium whitespace-nowrap text-muted-foreground";

export function MembersSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const { data: members = [], isLoading } = useProjectMembers(project.id);
  const remove = useRemoveProjectMember(project.id);
  const [assignOpen, setAssignOpen] = useState(false);
  const [confirmRemove, setConfirmRemove] = useState<ProjectMemberResponse | null>(null);
  const [roleChangeMember, setRoleChangeMember] = useState<ProjectMemberResponse | null>(null);

  const onRemove = (m: ProjectMemberResponse) => {
    remove.mutate(m.id, {
      onSuccess: () => {
        pushToast({ msg: "已移除成员", kind: "success" });
        setConfirmRemove(null);
      },
      onError: (err) => pushToast({ msg: "移除失败", sub: (err as Error).message }),
    });
  };

  return (
    <>
      <Card>
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3.5">
          <h3 className="text-sm font-semibold">项目成员</h3>
          <div className="flex items-center gap-1.5 whitespace-nowrap">
            <Button size="sm" variant="ghost" onClick={() => setAssignOpen(true)}>
              <Icon name="plus" />
              添加成员
            </Button>
          </div>
        </div>

        {isLoading && <div className={PLACEHOLDER_CLASS}>加载中...</div>}
        {!isLoading && members.length === 0 && (
          <div className={PLACEHOLDER_CLASS}>暂无成员，点击右上角按钮添加标注员或审核员</div>
        )}
        {!isLoading && members.length > 0 && (
          <div className="w-full overflow-x-auto">
            <table className="w-full min-w-[620px] border-separate border-spacing-0 text-sm">
              <thead>
                <tr>
                  {["成员", "角色", "加入时间", ""].map((h, i) => (
                    <th
                      key={i}
                      className={`${HEAD_CELL_BASE}${i === 0 ? " pl-4" : ""}${i === 3 ? " pr-4" : ""}`}
                    >
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.id}>
                    <td className="w-[42%] border-b border-border py-2.5 pr-3 pl-4">
                      <div className="flex min-w-0 items-center gap-2.5">
                        <UserAvatar
                          size="sm"
                          user={{
                            name: m.user_name,
                            email: m.user_email,
                            avatar_ref: m.avatar_ref,
                          }}
                        />
                        <div className="min-w-0">
                          <div className="truncate text-sm font-medium" title={m.user_name}>
                            {m.user_name}
                          </div>
                          <div
                            className="truncate text-xs text-muted-foreground"
                            title={m.user_email}
                          >
                            {m.user_email}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="border-b border-border p-3 whitespace-nowrap">
                      <Badge
                        variant={
                          m.role === "annotator"
                            ? "accent"
                            : m.role === "reviewer"
                              ? "warning"
                              : "outline"
                        }
                      >
                        {PROJECT_ROLE_LABELS[m.role as ProjectRole] ?? m.role}
                      </Badge>
                      <span className="mt-1 block text-2xs text-muted-foreground">
                        {ROLE_LABELS[m.platform_role as PlatformRole] ?? m.platform_role}
                      </span>
                    </td>
                    <td className="border-b border-border p-3 whitespace-nowrap text-muted-foreground">
                      {new Date(m.assigned_at).toLocaleDateString("zh-CN")}
                    </td>
                    <td className="border-b border-border py-2.5 pr-4 pl-3 text-right whitespace-nowrap">
                      <Button
                        size="sm"
                        variant="ghost"
                        title="更改职责"
                        onClick={() => setRoleChangeMember(m)}
                      >
                        <Icon name="refresh" size={11} />
                        更改职责
                      </Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirmRemove(m)}>
                        <Icon name="x" size={11} />
                        移除
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {assignOpen && (
        <AssignMemberModal
          open
          projectId={project.id}
          existing={members}
          onClose={() => setAssignOpen(false)}
        />
      )}

      <MemberRoleChangeModal
        open={!!roleChangeMember}
        projectId={project.id}
        member={roleChangeMember}
        members={members}
        onClose={() => setRoleChangeMember(null)}
      />

      <Modal
        open={!!confirmRemove}
        onClose={() => setConfirmRemove(null)}
        title="移除成员"
        width={420}
      >
        <div className="mb-[18px] text-sm text-muted-foreground">
          确认将 <strong className="text-foreground">{confirmRemove?.user_name}</strong>{" "}
          从本项目移除？该用户将不再看到此项目，已完成的标注/审核记录保留。
        </div>
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={() => setConfirmRemove(null)}>
            取消
          </Button>
          <Button
            variant="danger"
            disabled={remove.isPending}
            onClick={() => confirmRemove && onRemove(confirmRemove)}
          >
            {remove.isPending ? "处理中..." : "确认移除"}
          </Button>
        </div>
      </Modal>
    </>
  );
}
