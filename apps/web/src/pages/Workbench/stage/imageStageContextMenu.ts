import type { DropdownItem } from "@/components/ui/DropdownMenu";
import type { Annotation } from "@/types";
import { canJoinPolygonAnnotation } from "./shared/geometry/polygonOps";

export const IMAGE_CONTEXT_MENU_DRAG_THRESHOLD_PX = 5;

type NodeLike = {
  getAttr: (name: string) => unknown;
  getParent: () => NodeLike | null;
};

export interface ImageContextMenuClipboardActions {
  copyAnnotation: (annotation: Annotation) => number;
  paste: () => Promise<unknown> | unknown;
  hasClipboard: boolean;
}

interface BuildImageContextMenuItemsArgs {
  annotation: Annotation;
  readOnly: boolean;
  minZOrder: number;
  maxZOrder: number;
  clipboard: ImageContextMenuClipboardActions | null;
  selectedAnnotations?: Annotation[];
  onChangeClass?: (id: string) => void;
  onJoinSelected?: () => void;
  onCropSelected?: (baseId: string) => void;
  onSlicePolygon?: (annotation: Annotation) => void;
  sliceDisabledReason?: string | null;
  onDelete?: (id: string) => void;
  onPatchFlag?: (
    id: string,
    flag: "z_order" | "is_locked" | "is_hidden",
    value: number | boolean,
  ) => void;
  secondaryBarHidden?: boolean;
  onToggleSecondaryBar?: () => void;
}

export function didImageContextMenuDrag(
  down: { x: number; y: number } | null,
  point: { x: number; y: number },
  thresholdPx: number = IMAGE_CONTEXT_MENU_DRAG_THRESHOLD_PX,
): boolean {
  if (!down) return false;
  return Math.hypot(point.x - down.x, point.y - down.y) >= thresholdPx;
}

export function shouldSuppressImageContextMenu(args: {
  readOnly: boolean;
  keypointDraftPending: boolean;
  down: { x: number; y: number } | null;
  point: { x: number; y: number };
  thresholdPx?: number;
}): boolean {
  return (
    args.readOnly ||
    args.keypointDraftPending ||
    didImageContextMenuDrag(args.down, args.point, args.thresholdPx)
  );
}

export function findContextMenuAnnotationId(node: NodeLike | null): string | null {
  let cur = node;
  while (cur) {
    const id = cur.getAttr("id");
    if (typeof id === "string" && id.length > 0) return id;
    cur = cur.getParent();
  }
  return null;
}

export function buildImageContextMenuItems({
  annotation,
  readOnly,
  minZOrder,
  maxZOrder,
  clipboard,
  selectedAnnotations = [annotation],
  onChangeClass,
  onJoinSelected,
  onCropSelected,
  onSlicePolygon,
  sliceDisabledReason,
  onDelete,
  onPatchFlag,
  secondaryBarHidden,
  onToggleSecondaryBar,
}: BuildImageContextMenuItemsArgs): DropdownItem[] {
  const locked = Boolean(annotation.is_locked);
  const hidden = Boolean(annotation.is_hidden);
  const shapeMutationDisabled = readOnly || locked;
  const joinableSelected = selectedAnnotations.filter((item) =>
    item.geometry
      ? canJoinPolygonAnnotation({
          geometry: item.geometry,
          is_locked: item.is_locked,
        })
      : false,
  );
  const joinDisabled =
    readOnly ||
    !onJoinSelected ||
    joinableSelected.length < 2 ||
    new Set(joinableSelected.map((item) => item.cls)).size > 1 ||
    !joinableSelected.some((item) => item.id === annotation.id);
  const cropDisabled =
    readOnly ||
    !onCropSelected ||
    joinableSelected.length < 2 ||
    !joinableSelected.some((item) => item.id === annotation.id);

  const items: DropdownItem[] = [
    {
      id: "class",
      label: "改类别",
      icon: "tag",
      disabled: shapeMutationDisabled || !onChangeClass,
      onSelect: () => onChangeClass?.(annotation.id),
    },
    {
      id: "locked",
      label: locked ? "解锁" : "锁定",
      icon: locked ? "unlock" : "lock",
      kbd: "L",
      disabled: readOnly || !onPatchFlag,
      onSelect: () => onPatchFlag?.(annotation.id, "is_locked", !locked),
    },
    {
      id: "hidden",
      label: hidden ? "显示" : "隐藏",
      icon: hidden ? "eyeOff" : "eye",
      kbd: "H",
      disabled: shapeMutationDisabled || !onPatchFlag,
      onSelect: () => onPatchFlag?.(annotation.id, "is_hidden", !hidden),
    },
    {
      id: "join",
      label: "合并多边形",
      icon: "layers",
      disabled: joinDisabled,
      onSelect: () => onJoinSelected?.(),
    },
    {
      id: "crop",
      label: "裁切重叠区",
      icon: "scissors",
      disabled: cropDisabled,
      onSelect: () => onCropSelected?.(annotation.id),
    },
  ];

  if (
    onSlicePolygon &&
    (annotation.geometry?.type === "polygon" || annotation.geometry?.type === "multi_polygon")
  ) {
    items.push({
      id: "slice-polygon",
      label: sliceDisabledReason ? `切割多边形（${sliceDisabledReason}）` : "切割多边形",
      icon: "scissors",
      disabled: shapeMutationDisabled || !!sliceDisabledReason,
      onSelect: () => onSlicePolygon(annotation),
    });
  }

  items.push(
    { id: "state-divider", divider: true, label: "" },
    {
      id: "z-top",
      label: "移到顶层",
      disabled: shapeMutationDisabled || !onPatchFlag,
      onSelect: () => onPatchFlag?.(annotation.id, "z_order", maxZOrder + 1),
    },
    {
      id: "z-bottom",
      label: "移到底层",
      disabled: shapeMutationDisabled || !onPatchFlag,
      onSelect: () => onPatchFlag?.(annotation.id, "z_order", minZOrder - 1),
    },
    { id: "edit-divider", divider: true, label: "" },
    {
      id: "copy",
      label: "复制",
      icon: "copy",
      kbd: "Ctrl+C",
      disabled: !clipboard || annotation.geometry?.type === "raster_mask",
      onSelect: () => {
        clipboard?.copyAnnotation(annotation);
      },
    },
    {
      id: "paste",
      label: "粘贴",
      icon: "clipboardPaste",
      kbd: "Ctrl+V",
      disabled: readOnly || !clipboard?.hasClipboard,
      onSelect: () => {
        void clipboard?.paste();
      },
    },
    { id: "delete-divider", divider: true, label: "" },
    {
      id: "delete",
      label: "删除",
      icon: "trash",
      kbd: "Del",
      disabled: shapeMutationDisabled || !onDelete,
      onSelect: () => onDelete?.(annotation.id),
    },
  );

  if (onToggleSecondaryBar) {
    items.push(
      { id: "secondary-bar-divider", divider: true, label: "" },
      {
        id: "toggle-secondary-bar",
        label: secondaryBarHidden ? "打开二次推理面板" : "关闭二次推理面板",
        icon: "sparkle",
        onSelect: () => onToggleSecondaryBar(),
      },
    );
  }

  return items;
}
