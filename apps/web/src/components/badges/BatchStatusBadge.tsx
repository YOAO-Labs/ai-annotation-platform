import { Badge } from "@/components/ui/Badge";
import { Icon, type IconName } from "@/components/ui/Icon";

/** 与 apps/api/app/db/enums.py BatchStatus 对齐. */
export type BatchStatus =
  | "draft"
  | "active"
  | "pre_annotated"
  | "annotating"
  | "reviewing"
  | "approved"
  | "rejected"
  | "archived";

type BadgeVariant = "default" | "accent" | "warning" | "success" | "danger" | "ai" | "outline";

interface StatusMeta {
  label: string;
  variant: BadgeVariant;
  icon?: IconName;
}

const STATUS_META: Record<BatchStatus, StatusMeta> = {
  draft: { label: "草稿", variant: "default" },
  active: { label: "激活", variant: "accent" },
  pre_annotated: { label: "AI 预标已就绪", variant: "ai", icon: "sparkles" },
  annotating: { label: "标注中", variant: "accent" },
  reviewing: { label: "审核中", variant: "warning" },
  approved: { label: "已通过", variant: "success" },
  rejected: { label: "已退回", variant: "danger" },
  archived: { label: "已归档", variant: "default" },
};

interface Props {
  status: BatchStatus | string;
  /** 紧凑模式：仅显示文字 + variant（无 icon）。 */
  compact?: boolean;
}

/**
 * 统一 batch 状态徽章; pre_annotated 时紫色 + sparkles icon
 * 让标注员 / admin 一眼知道「AI 预标已就绪, 先看 AI 候选」.
 */
export function BatchStatusBadge({ status, compact }: Props) {
  const meta = STATUS_META[status as BatchStatus] ?? {
    label: status,
    variant: "default" as BadgeVariant,
  };
  return (
    <Badge variant={meta.variant}>
      {!compact && meta.icon && <Icon name={meta.icon} size={10} />}
      {meta.label}
    </Badge>
  );
}
