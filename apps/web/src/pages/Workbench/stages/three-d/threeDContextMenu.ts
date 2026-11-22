import type { DropdownItem } from "@/components/ui/DropdownMenu";

export interface ThreeDBoxContextMenuArgs {
  readOnly: boolean;
  locked: boolean;
  hidden: boolean;
  hasClipboard: boolean;
  canPropagate: boolean;
  canInterpolate: boolean;
  onPropagateNext: () => void;
  onPropagatePrev: () => void;
  onPropagateToFrame: () => void;
  onInterpolate: () => void;
  onChangeClass: () => void;
  onToggleLock: () => void;
  onToggleHidden: () => void;
  onCopy: () => void;
  onPaste: () => void;
  onDelete: () => void;
}

export function buildThreeDBoxContextMenuItems(a: ThreeDBoxContextMenuArgs): DropdownItem[] {
  const mutationDisabled = a.readOnly || a.locked;
  const items: DropdownItem[] = [];

  if (a.canPropagate) {
    items.push(
      {
        id: "propagate-next",
        label: "延续到下一帧",
        kbd: "Shift+→",
        disabled: a.readOnly,
        onSelect: a.onPropagateNext,
      },
      {
        id: "propagate-prev",
        label: "延续到上一帧",
        kbd: "Shift+←",
        disabled: a.readOnly,
        onSelect: a.onPropagatePrev,
      },
      {
        id: "propagate-to-frame",
        label: "延续到指定帧…",
        disabled: a.readOnly,
        onSelect: a.onPropagateToFrame,
      },
      {
        id: "interpolate",
        label: "插值填充到指定帧…",
        disabled: a.readOnly || !a.canInterpolate,
        onSelect: a.onInterpolate,
      },
      { id: "propagate-divider", divider: true, label: "" },
    );
  }

  items.push(
    {
      id: "class",
      label: "改类别",
      icon: "tag",
      disabled: mutationDisabled,
      onSelect: a.onChangeClass,
    },
    {
      id: "lock",
      label: a.locked ? "解锁" : "锁定",
      icon: a.locked ? "unlock" : "lock",
      disabled: a.readOnly,
      onSelect: a.onToggleLock,
    },
    {
      id: "hidden",
      label: a.hidden ? "显示" : "隐藏",
      icon: a.hidden ? "eyeOff" : "eye",
      disabled: mutationDisabled,
      onSelect: a.onToggleHidden,
    },
    {
      id: "copy",
      label: "复制",
      icon: "copy",
      kbd: "Ctrl+C",
      onSelect: a.onCopy,
    },
    {
      id: "paste",
      label: "粘贴",
      icon: "clipboardPaste",
      kbd: "Ctrl+V",
      disabled: a.readOnly || !a.hasClipboard,
      onSelect: a.onPaste,
    },
    { id: "delete-divider", divider: true, label: "" },
    {
      id: "delete",
      label: "删除",
      icon: "trash",
      kbd: "Del",
      disabled: mutationDisabled,
      onSelect: a.onDelete,
    },
  );

  return items;
}

export interface ThreeDEmptyContextMenuArgs {
  readOnly: boolean;
  hasClipboard: boolean;
  canPropagate: boolean;
  onPropagateBatchNext: () => void;
  onPropagateBatchPrev: () => void;
  onPaste: () => void;
}

export function buildThreeDEmptyContextMenuItems(a: ThreeDEmptyContextMenuArgs): DropdownItem[] {
  const items: DropdownItem[] = [];

  if (a.canPropagate) {
    items.push(
      {
        id: "batch-next",
        label: "批量延续到下一帧",
        kbd: "Ctrl+Shift+→",
        disabled: a.readOnly,
        onSelect: a.onPropagateBatchNext,
      },
      {
        id: "batch-prev",
        label: "批量延续到上一帧",
        kbd: "Ctrl+Shift+←",
        disabled: a.readOnly,
        onSelect: a.onPropagateBatchPrev,
      },
    );
  }

  if (a.hasClipboard) {
    if (items.length > 0) items.push({ id: "paste-divider", divider: true, label: "" });
    items.push({
      id: "paste",
      label: "粘贴",
      icon: "clipboardPaste",
      kbd: "Ctrl+V",
      disabled: a.readOnly,
      onSelect: a.onPaste,
    });
  }

  return items;
}
