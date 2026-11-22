import type { MaskEditBlockReason, MaskEditorPhase } from "./canEditMask";
import type {
  MaskInstanceOperationPreview,
  MaskOperationPreview,
  MaskOperationStatus,
} from "./useMaskEditor";

export type MaskPrimaryActionKind =
  | "none"
  | "save"
  | "commit_instances"
  | "apply_region"
  | "recover_session"
  | "recover_operation"
  | "refresh_instances"
  | "retry_instances"
  | "cancel_preview";

export type MaskSecondaryActionKind = "cancel_preview" | "exit" | "none";

export interface MaskPrimaryActions {
  primary: {
    kind: MaskPrimaryActionKind;
    label: string;
    disabled: boolean;
    description: string;
  };
  secondary: {
    kind: MaskSecondaryActionKind;
    label: string;
    disabled: boolean;
  };
  hint: string;
}

export interface MaskPrimaryActionsInput {
  active: boolean;
  phase: MaskEditorPhase;
  dirty: boolean;
  canEdit: boolean;
  canCommit?: boolean;
  interactionFrozen?: boolean;
  revision: number;
  operationStatus: MaskOperationStatus;
  operationPreview: MaskOperationPreview | null;
  instanceOperationPreview: MaskInstanceOperationPreview | null;
  operationError?: unknown;
  instanceCommitting?: boolean;
  instanceRefreshing?: boolean;
  instanceCommitError?: string | null;
  instanceCanRetry?: boolean;
  instanceCanRefresh?: boolean;
  instanceCommitBlocked?: boolean;
  savePending?: boolean;
  saveLabel?: string;
  saveHint?: string;
  editBlockReason?: MaskEditBlockReason | null;
}

export const MASK_EDIT_BLOCK_REASON_LABELS: Record<MaskEditBlockReason, string> = {
  task_read_only: "任务只读或原生 Mask 写能力未开启",
  annotation_locked: "当前标注已锁定",
  track_locked: "当前 Mask 轨迹已锁定",
  segment_locked: "当前视频分段锁冲突",
  editor_idle: "请先进入 Mask 编辑",
  editor_loading: "正在加载 Mask",
  editor_saving: "正在保存 Mask",
  editor_error: "请先恢复失败的编辑会话",
  large_canvas_budget_exceeded: "当前设备无法容纳可见分块，请放大局部 ROI 或更换高内存设备",
};

const WRITE_LOCK_REASONS = new Set<MaskEditBlockReason>([
  "task_read_only",
  "annotation_locked",
  "track_locked",
  "segment_locked",
]);

export function resolveMaskPrimaryActions(input: MaskPrimaryActionsInput): MaskPrimaryActions {
  const region = input.operationPreview;
  const instance = input.instanceOperationPreview;
  const hasPreview = Boolean(region || instance);
  const hasInstanceFailure = Boolean(input.instanceCommitError || input.instanceCommitBlocked);
  const cancelPreview: MaskPrimaryActions["secondary"] = {
    kind: "cancel_preview",
    label: "取消预览",
    disabled: false,
  };
  const exit: MaskPrimaryActions["secondary"] = {
    kind: "exit",
    label: "退出编辑",
    disabled: false,
  };
  const blockedSecondary: MaskPrimaryActions["secondary"] = {
    kind: "none",
    label: hasPreview ? "取消预览" : "退出编辑",
    disabled: true,
  };
  const blockDescription = input.editBlockReason
    ? MASK_EDIT_BLOCK_REASON_LABELS[input.editBlockReason]
    : "当前 Mask 暂不可编辑";
  const writeLocked = Boolean(
    input.editBlockReason && WRITE_LOCK_REASONS.has(input.editBlockReason),
  );
  const result = (
    kind: MaskPrimaryActionKind,
    label: string,
    description: string,
    options: {
      disabled?: boolean;
      secondary?: MaskPrimaryActions["secondary"];
      hint?: string;
    } = {},
  ): MaskPrimaryActions => ({
    primary: { kind, label, disabled: kind === "none" || !!options.disabled, description },
    secondary:
      options.secondary ??
      (hasPreview || hasInstanceFailure || input.operationStatus === "error"
        ? cancelPreview
        : exit),
    hint: options.hint ?? description,
  });

  if (input.interactionFrozen) {
    return result("none", "编辑已暂停", "当前视图暂不接受 Mask 操作。", {
      secondary: blockedSecondary,
    });
  }
  if (input.instanceRefreshing || input.instanceCommitting || input.phase === "saving") {
    const label = input.instanceRefreshing
      ? "刷新中…"
      : input.instanceCommitting
        ? "提交中…"
        : "保存中…";
    return result("none", label, "正在处理 Mask，请等待完成后再操作。", {
      secondary: blockedSecondary,
    });
  }
  if (input.savePending) {
    return result("none", "准备保存…", "正在准备 Mask 或等待类别确认，请完成当前步骤。", {
      secondary: blockedSecondary,
    });
  }
  if (input.phase === "loading") {
    return result("none", "加载中…", "正在加载当前 Mask。", { secondary: blockedSecondary });
  }
  if (input.operationStatus === "computing") {
    return result("none", "计算中…", "正在处理 Mask 运算，尚未保存。", {
      secondary: { ...cancelPreview, label: "取消运算" },
      hint: "Esc 取消本次运算，保留已有像素草稿。",
    });
  }

  if (region && instance) {
    return result("recover_operation", "清除冲突预览", "区域与实例预览冲突，请清除后重新生成。", {
      secondary: cancelPreview,
    });
  }
  const staleInstance = Boolean(
    instance && (!input.active || instance.sourceRevision !== input.revision),
  );
  if (
    staleInstance ||
    hasInstanceFailure ||
    (instance && (input.phase === "error" || input.operationStatus === "error"))
  ) {
    const description = staleInstance
      ? "实例预览已失效，请重新生成后提交；已有像素草稿保留。"
      : (input.instanceCommitError ?? "实例预览暂不能提交，已有草稿保留。");
    if (
      !staleInstance &&
      instance &&
      input.instanceCanRetry &&
      !input.instanceCommitBlocked &&
      !writeLocked
    ) {
      return result("retry_instances", "重试实例提交", description);
    }
    if (input.instanceCanRefresh) {
      return result("refresh_instances", "刷新范围", description);
    }
    return result("cancel_preview", "取消失效预览", description);
  }
  if (instance) {
    const disabled = !input.canEdit || writeLocked;
    return result(
      "commit_instances",
      `提交 ${instance.plan.resultCount} 个实例`,
      disabled ? blockDescription : "一次提交全部实例变更，直接保存到当前任务。",
      {
        disabled,
        hint: disabled
          ? blockDescription
          : "实例预览尚未保存。Enter 原子提交；Esc 取消预览，保留编辑会话。",
      },
    );
  }
  if (region && (!input.active || region.sourceRevision !== input.revision)) {
    return result(
      "recover_operation",
      "清除失效预览",
      "区域预览已失效，请重新生成；已有像素草稿保留。",
    );
  }
  if (input.operationStatus === "error" || (input.operationStatus === "preview" && !region)) {
    const message =
      input.operationError instanceof Error
        ? input.operationError.message
        : typeof input.operationError === "string"
          ? input.operationError
          : "";
    return result(
      "recover_operation",
      "恢复编辑",
      message || "运算预览已失效，请清除后重新生成；已有像素草稿保留。",
      { secondary: cancelPreview },
    );
  }
  if (input.phase === "error") {
    return result(
      "recover_session",
      "恢复编辑",
      region
        ? "先恢复 Mask 会话，再应用区域预览。"
        : "恢复当前 Mask 会话后，可继续编辑或再次保存。",
    );
  }
  if (region) {
    const disabled = !input.canEdit || writeLocked;
    return result(
      "apply_region",
      "应用区域预览",
      disabled ? blockDescription : "只更新像素草稿，随后保存才会写入标注。",
      {
        disabled,
        hint: disabled
          ? blockDescription
          : "区域预览尚未应用。Enter 应用到草稿；Esc 取消预览，保留此前笔画。",
      },
    );
  }
  if (!input.active || input.phase === "idle") {
    return result("none", "尚未编辑", "在画布绘制后即可保存 Mask。", { secondary: exit });
  }
  if (input.dirty) {
    const disabled = !(input.canCommit ?? input.canEdit) || writeLocked;
    return result(
      "save",
      input.saveLabel ?? "保存 Mask",
      disabled ? blockDescription : (input.saveHint ?? "将当前像素草稿保存到标注。"),
      {
        disabled,
        hint: disabled
          ? `${blockDescription}。已有像素草稿保留。`
          : `${input.saveHint ? `${input.saveHint} ` : ""}Enter 保存；Esc 退出前选择保存、丢弃或继续编辑。`,
      },
    );
  }
  return result("none", "已保存", "当前没有像素修改，无需提交。", {
    hint: `${input.saveHint ? `${input.saveHint} ` : ""}Enter 不新建标注或关键帧；Esc 退出编辑。`,
  });
}
