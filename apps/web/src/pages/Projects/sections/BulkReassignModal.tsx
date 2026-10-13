import { useMemo, useState } from "react";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { UserAvatar } from "@/components/ui/UserAvatar";
import { Icon } from "@/components/ui/Icon";
import { Badge } from "@/components/ui/Badge";
import { useProjectMembers } from "@/hooks/useProjects";
import type { BulkBatchActionResponse } from "@/api/batches";

interface Props {
  projectId: string;
  count: number;
  onClose: () => void;
  onSubmit: (payload: {
    annotator_id?: string | null;
    reviewer_id?: string | null;
  }) => Promise<BulkBatchActionResponse | void>;
  pending?: boolean;
}

type Sentinel = "__keep__" | "__clear__";

function cn(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

/**
 * v0.7.3 · 批量改派 modal。
 * 标注员 / 审核员各一栏：保留不变 / 清空 / 选某成员，三种语义。
 */
export function BulkReassignModal({ projectId, count, onClose, onSubmit, pending }: Props) {
  const { data: members = [], isLoading } = useProjectMembers(projectId);

  // 「保留不变」= 不发送该字段；「清空」= 发送 null；选成员 = user_id
  const [annotatorChoice, setAnnotatorChoice] = useState<string | Sentinel>("__keep__");
  const [reviewerChoice, setReviewerChoice] = useState<string | Sentinel>("__keep__");

  const annotators = useMemo(() => members.filter((m) => m.role === "annotator"), [members]);
  const reviewers = useMemo(() => members.filter((m) => m.role === "reviewer"), [members]);

  const dirty = annotatorChoice !== "__keep__" || reviewerChoice !== "__keep__";

  const handleSubmit = async () => {
    const payload: { annotator_id?: string | null; reviewer_id?: string | null } = {};
    if (annotatorChoice !== "__keep__") {
      payload.annotator_id = annotatorChoice === "__clear__" ? null : annotatorChoice;
    }
    if (reviewerChoice !== "__keep__") {
      payload.reviewer_id = reviewerChoice === "__clear__" ? null : reviewerChoice;
    }
    await onSubmit(payload);
  };

  return (
    <Modal open onClose={onClose} title={`批量改派 · 已选 ${count} 个批次`} width={560}>
      <div className="mb-3 text-sm text-muted-foreground">
        留空或选择「保留不变」则该字段不会被修改；选择「清空指派」则该字段会被设为未分派。
      </div>

      {isLoading && <div className="p-4 text-center text-sm text-muted-foreground">加载成员…</div>}

      {!isLoading && (
        <div className="grid grid-cols-[repeat(auto-fit,minmax(220px,1fr))] gap-3">
          <Column
            title="标注员"
            members={annotators}
            choice={annotatorChoice}
            onChange={setAnnotatorChoice}
            roleColor="accent"
          />
          <Column
            title="审核员"
            members={reviewers}
            choice={reviewerChoice}
            onChange={setReviewerChoice}
            roleColor="warning"
          />
        </div>
      )}

      <div className="mt-4 flex justify-end gap-2">
        <Button onClick={onClose}>取消</Button>
        <Button variant="primary" onClick={handleSubmit} disabled={pending || !dirty}>
          {pending ? "提交中…" : `确认改派 ${count} 个批次`}
        </Button>
      </div>
    </Modal>
  );
}

function Column({
  title,
  members,
  choice,
  onChange,
  roleColor,
}: {
  title: string;
  members: {
    id: string;
    user_id: string;
    user_name: string;
    user_email: string;
    role: string;
    /** 头像引用；结构上是 ProjectMemberResponse 的子集，缺省 = 回退首字母。 */
    avatar_ref?: string | null;
  }[];
  choice: string | Sentinel;
  onChange: (v: string | Sentinel) => void;
  roleColor: "accent" | "warning";
}) {
  const renderRow = (
    key: string,
    label: React.ReactNode,
    value: string | Sentinel,
    sub?: string,
    icon?: React.ReactNode,
  ) => {
    const checked = choice === value;
    return (
      <button
        key={key}
        type="button"
        onClick={() => onChange(value)}
        className={cn(
          "mb-0.5 flex w-full cursor-pointer appearance-none items-center gap-2 rounded-sm border border-transparent bg-transparent px-2 py-1.5 text-left text-foreground [font:inherit]",
          checked && "border-brand bg-brand/10",
        )}
      >
        <span
          className={cn(
            "relative size-3.5 flex-shrink-0 rounded-full border border-border bg-background",
            checked && "bg-brand",
          )}
        >
          {checked && <span className="absolute inset-[3px] rounded-full bg-white" />}
        </span>
        {icon}
        <span className="min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap">
          <span className="text-sm font-medium">{label}</span>
          {sub && <span className="ml-1.5 text-xs text-muted-foreground">{sub}</span>}
        </span>
      </button>
    );
  };

  return (
    <div className="max-h-[300px] overflow-y-auto rounded-md border border-border bg-muted p-2">
      <div className="px-1.5 pb-2 pt-1">
        <Badge variant={roleColor} dot>
          {title}
        </Badge>
      </div>
      {renderRow("__keep__", "保留不变", "__keep__", "（不修改该字段）")}
      {renderRow(
        "__clear__",
        "清空指派",
        "__clear__",
        "（设为未分派）",
        <Icon name="x" size={11} />,
      )}
      <div className="mx-1.5 my-1 h-px bg-border" />
      {members.length === 0 && (
        <div className="p-4 text-center text-xs text-muted-foreground">暂无成员</div>
      )}
      {members.map((m) =>
        renderRow(
          m.id,
          m.user_name,
          m.user_id,
          m.user_email,
          <UserAvatar
            size="sm"
            user={{ name: m.user_name, email: m.user_email, avatar_ref: m.avatar_ref }}
          />,
        ),
      )}
    </div>
  );
}
