import { UserAvatar } from "@/components/ui/UserAvatar";

/**
 * AssigneeAvatarStack —— 责任人头像组(v0.17.2,module.css → Tailwind)。
 * 复用迁好的 Avatar;头像叠放用负 margin + 同色描边环。
 */
export interface AssigneeBrief {
  id: string;
  name: string;
  email?: string;
  role?: string | null;
  avatar_initial?: string;
  avatar_ref?: string | null;
}

interface Props {
  users: AssigneeBrief[];
  max?: number;
  size?: "sm" | "md";
  /** 标签前缀,例如「标注员」「审核员」;不传不渲染 */
  label?: string;
  /** 0 用户时是否显示「未分派」灰条 */
  emptyHint?: string;
  title?: string;
}

export function AssigneeAvatarStack({
  users,
  max = 3,
  size = "sm",
  label,
  emptyHint = "未分派",
  title,
}: Props) {
  if (users.length === 0) {
    return (
      <span className="text-xs italic text-muted-foreground">
        {label ? `${label}:` : ""}
        {emptyHint}
      </span>
    );
  }

  const visible = users.slice(0, max);
  const overflow = users.length - visible.length;
  const tooltip = title ?? users.map((u) => u.name).join("、");

  return (
    <span title={tooltip} className="inline-flex shrink-0 items-center gap-1.5">
      {label && <span className="whitespace-nowrap text-xs text-muted-foreground">{label}</span>}
      <span className="inline-flex">
        {visible.map((u) => (
          <span
            key={u.id}
            className="rounded-full border-[1.5px] border-card bg-card [&:not(:first-child)]:-ml-1.5"
          >
            <UserAvatar
              user={{
                id: u.id,
                name: u.name,
                email: u.email,
                avatar_initial: u.avatar_initial,
                avatar_ref: u.avatar_ref,
              }}
              size={size}
            />
          </span>
        ))}
      </span>
      {overflow > 0 && <span className="ml-0.5 text-xs text-muted-foreground">+{overflow}</span>}
    </span>
  );
}
