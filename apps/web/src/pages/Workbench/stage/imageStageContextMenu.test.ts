import { describe, expect, it, vi } from "vitest";
import type { Annotation } from "@/types";
import {
  buildImageContextMenuItems,
  didImageContextMenuDrag,
  findContextMenuAnnotationId,
  shouldSuppressImageContextMenu,
} from "./imageStageContextMenu";

function annotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: "ann-1",
    cls: "car",
    x: 0.1,
    y: 0.1,
    w: 0.2,
    h: 0.2,
    conf: 1,
    source: "manual",
    geometry: { type: "bbox", x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    ...overrides,
  } as Annotation;
}

describe("imageStageContextMenu", () => {
  it("treats right-button movement >= threshold as drag", () => {
    expect(didImageContextMenuDrag({ x: 10, y: 10 }, { x: 15, y: 14 })).toBe(true);
    expect(didImageContextMenuDrag({ x: 10, y: 10 }, { x: 13, y: 13 })).toBe(false);
  });

  it("adds a secondary-bar toggle item only when onToggleSecondaryBar given", () => {
    const withoutToggle = buildImageContextMenuItems({
      annotation: annotation(),
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
    });
    expect(withoutToggle.find((i) => i.id === "toggle-secondary-bar")).toBeUndefined();

    const onToggleSecondaryBar = vi.fn();
    const hidden = buildImageContextMenuItems({
      annotation: annotation(),
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
      secondaryBarHidden: true,
      onToggleSecondaryBar,
    });
    const item = hidden.find((i) => i.id === "toggle-secondary-bar");
    expect(item?.label).toBe("打开二次推理面板");
    item?.onSelect?.();
    expect(onToggleSecondaryBar).toHaveBeenCalledTimes(1);

    const shown = buildImageContextMenuItems({
      annotation: annotation(),
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
      secondaryBarHidden: false,
      onToggleSecondaryBar,
    });
    expect(shown.find((i) => i.id === "toggle-secondary-bar")?.label).toBe("关闭二次推理面板");
  });

  it("suppresses the menu during readOnly, keypoint drafting, or drag", () => {
    expect(
      shouldSuppressImageContextMenu({
        readOnly: true,
        keypointDraftPending: false,
        down: { x: 0, y: 0 },
        point: { x: 0, y: 0 },
      }),
    ).toBe(true);
    expect(
      shouldSuppressImageContextMenu({
        readOnly: false,
        keypointDraftPending: true,
        down: { x: 0, y: 0 },
        point: { x: 0, y: 0 },
      }),
    ).toBe(true);
    expect(
      shouldSuppressImageContextMenu({
        readOnly: false,
        keypointDraftPending: false,
        down: { x: 0, y: 0 },
        point: { x: 6, y: 0 },
      }),
    ).toBe(true);
  });

  it("walks up hit-node parents to find the annotation id", () => {
    const root = {
      getAttr: (name: string) => (name === "id" ? "ann-42" : undefined),
      getParent: () => null,
    };
    const child = {
      getAttr: () => undefined,
      getParent: () => root,
    };
    expect(findContextMenuAnnotationId(child)).toBe("ann-42");
    expect(findContextMenuAnnotationId(null)).toBeNull();
  });

  it("builds bbox menu items with clipboard actions", () => {
    const onChangeClass = vi.fn();
    const onDelete = vi.fn();
    const onPatchFlag = vi.fn();
    const copyAnnotation = vi.fn();
    const paste = vi.fn();

    const items = buildImageContextMenuItems({
      annotation: annotation(),
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 3,
      clipboard: { copyAnnotation, paste, hasClipboard: true },
      onChangeClass,
      onDelete,
      onPatchFlag,
    });

    expect(items.find((item) => item.id === "paste")?.disabled).toBe(false);

    items.find((item) => item.id === "copy")?.onSelect?.();
    items.find((item) => item.id === "paste")?.onSelect?.();
    expect(copyAnnotation).toHaveBeenCalledWith(expect.objectContaining({ id: "ann-1" }));
    expect(paste).toHaveBeenCalled();
  });

  it("enables join only for selected same-class polygon annotations", () => {
    const onJoinSelected = vi.fn();
    const poly = annotation({
      geometry: {
        type: "polygon",
        points: [
          [0, 0],
          [0.1, 0],
          [0.1, 0.1],
        ],
      },
    });
    const peer = annotation({
      id: "ann-2",
      geometry: {
        type: "polygon",
        points: [
          [0.1, 0],
          [0.2, 0],
          [0.2, 0.1],
        ],
      },
    });

    const enabled = buildImageContextMenuItems({
      annotation: poly,
      selectedAnnotations: [poly, peer],
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
      onJoinSelected,
    }).find((item) => item.id === "join");

    expect(enabled?.disabled).toBe(false);
    enabled?.onSelect?.();
    expect(onJoinSelected).toHaveBeenCalled();

    const mixedClass = buildImageContextMenuItems({
      annotation: poly,
      selectedAnnotations: [poly, { ...peer, cls: "person" }],
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
      onJoinSelected,
    }).find((item) => item.id === "join");

    expect(mixedClass?.disabled).toBe(true);
  });

  it("enables crop for the right-clicked polygon plus a cutter, ignoring class match", () => {
    const onCropSelected = vi.fn();
    const base = annotation({
      geometry: {
        type: "polygon",
        points: [
          [0, 0],
          [0.1, 0],
          [0.1, 0.1],
        ],
      },
    });
    const cutter = annotation({
      id: "ann-2",
      cls: "person",
      geometry: {
        type: "polygon",
        points: [
          [0.05, 0],
          [0.2, 0],
          [0.2, 0.1],
        ],
      },
    });

    const enabled = buildImageContextMenuItems({
      annotation: base,
      selectedAnnotations: [base, cutter],
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
      onCropSelected,
    }).find((item) => item.id === "crop");

    expect(enabled?.disabled).toBe(false);
    enabled?.onSelect?.();
    expect(onCropSelected).toHaveBeenCalledWith(base.id);

    const noCutter = buildImageContextMenuItems({
      annotation: base,
      selectedAnnotations: [base],
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: null,
      onCropSelected,
    }).find((item) => item.id === "crop");

    expect(noCutter?.disabled).toBe(true);
  });

  it("disables mutations on locked shapes except unlock", () => {
    const items = buildImageContextMenuItems({
      annotation: annotation({
        geometry: {
          type: "keypoint",
          points: [{ x: 0.1, y: 0.2, v: 2 }],
        },
        keypoints: [{ x: 0.1, y: 0.2, v: 2 }],
        is_locked: true,
      }),
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard: { copyAnnotation: vi.fn(), paste: vi.fn(), hasClipboard: false },
      onChangeClass: vi.fn(),
      onDelete: vi.fn(),
      onPatchFlag: vi.fn(),
    });

    expect(items.find((item) => item.id === "class")?.disabled).toBe(true);
    expect(items.find((item) => item.id === "hidden")?.disabled).toBe(true);
    expect(items.find((item) => item.id === "delete")?.disabled).toBe(true);
    expect(items.find((item) => item.id === "locked")?.label).toBe("解锁");
    expect(items.find((item) => item.id === "locked")?.disabled).toBe(false);
  });

  it("disables raster_mask copy before native canvas support", () => {
    const clipboard = { copyAnnotation: vi.fn(), paste: vi.fn(), hasClipboard: false };
    const items = buildImageContextMenuItems({
      annotation: annotation({
        geometry: {
          type: "raster_mask",
          mask: {
            encoding: "coco_rle_ref",
            size: [10, 20],
            object_key: "raster-masks/sha256/aa/bb/digest.json",
            sha256: "a".repeat(64),
            runs: 4,
            bytes: 32,
          },
        },
      }),
      readOnly: false,
      minZOrder: 0,
      maxZOrder: 0,
      clipboard,
    });

    expect(items.find((item) => item.id === "copy")?.disabled).toBe(true);
    expect(clipboard.copyAnnotation).not.toHaveBeenCalled();
  });
});
