import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { Annotation } from "@/types";
import { buildAnnotationCommentBadges, ImageStageCommentBadges } from "./ImageStageCommentBadges";

function annotation(id: string, overrides: Partial<Annotation> = {}): Annotation {
  return {
    id,
    source: "manual",
    cls: "car",
    conf: 1,
    x: 0.1,
    y: 0.2,
    w: 0.3,
    h: 0.2,
    geometry: { type: "bbox", x: 0.1, y: 0.2, w: 0.3, h: 0.2 },
    ...overrides,
  };
}

const view = { scale: 2, tx: 10, ty: 20 };

describe("ImageStageCommentBadges", () => {
  it.each([
    [0, null],
    [1, "1"],
    [9, "9"],
    [10, "9+"],
  ])("renders the right compact label for %s comments", (count, label) => {
    const models = buildAnnotationCommentBadges({
      annotations: [annotation("ann-1")],
      counts: { "ann-1": count },
      imgW: 100,
      imgH: 80,
      vp: view,
    });
    expect(models[0]?.label ?? null).toBe(label);
    expect(models[0]?.count ?? null).toBe(count || null);
  });

  it("keeps separate positive counts for annotations with the same class", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [annotation("ann-1"), annotation("ann-2")],
      counts: { "ann-1": 1, "ann-2": 9 },
      imgW: 100,
      imgH: 80,
      vp: view,
    });
    expect(models.map(({ id, count }) => ({ id, count }))).toEqual([
      { id: "ann-1", count: 1 },
      { id: "ann-2", count: 9 },
    ]);
  });

  it("keeps hidden annotations out while ignoring label visibility", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [annotation("visible"), annotation("hidden", { is_hidden: true })],
      counts: { visible: 2, hidden: 3 },
      imgW: 100,
      imgH: 80,
      vp: view,
    });
    expect(models.map((model) => model.id)).toEqual(["visible"]);
  });

  it("uses a ready raster record bounds and skips a mask without bounds", () => {
    const raster = annotation("mask", {
      geometry: { type: "raster_mask", mask: {} as never },
      x: 0,
      y: 0,
      w: 0,
      h: 0,
    });
    const models = buildAnnotationCommentBadges({
      annotations: [
        raster,
        annotation("missing", { geometry: { type: "raster_mask", mask: {} as never } }),
      ],
      rasterMaskRecords: [{ id: "mask", bounds: { x: 0.4, y: 0.25, w: 0.1, h: 0.2 } }],
      counts: { mask: 1, missing: 1 },
      imgW: 1000,
      imgH: 800,
      vp: view,
    });
    expect(models).toHaveLength(1);
    expect(models[0]).toMatchObject({ id: "mask", left: 1_038, top: 392 });
  });

  it("keeps zero-area vector anchors for lines and single visible keypoints", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [
        annotation("line", {
          geometry: {
            type: "polyline",
            points: [
              [0.4, 0.5],
              [0.8, 0.5],
            ],
          },
        }),
        annotation("point", {
          geometry: { type: "keypoint", points: [{ x: 0.7, y: 0.6, v: 2 }] },
        }),
        annotation("legacy", {
          x: 0.2,
          y: 0.3,
          w: 0,
          h: 0,
          geometry: undefined,
        }),
      ],
      counts: { line: 1, point: 1, legacy: 1 },
      imgW: 100,
      imgH: 80,
      vp: view,
    });
    expect(models).toHaveLength(3);
    expect(models.map(({ id }) => id)).toEqual(["line", "point", "legacy"]);
  });

  it("anchors from the live geometry rather than stale annotation bounds", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [
        annotation("moved", {
          x: 0.1,
          y: 0.2,
          w: 0.3,
          h: 0.2,
          geometry: { type: "bbox", x: 0.5, y: 0.4, w: 0.1, h: 0.1 },
        }),
      ],
      counts: { moved: 1 },
      imgW: 100,
      imgH: 80,
      vp: { scale: 1, tx: 0, ty: 0 },
    });
    expect(models[0]).toMatchObject({ left: 88, top: 4 });
  });

  it("uses image dimensions when rotating a non-square annotation", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [
        annotation("rotated", {
          geometry: { type: "rotated_bbox", cx: 0.5, cy: 0.5, w: 0.2, h: 0.4, angle: 90 },
        }),
      ],
      counts: { rotated: 1 },
      imgW: 200,
      imgH: 100,
      vp: { scale: 1, tx: 0, ty: 0 },
    });
    expect(models[0]).toMatchObject({ left: 148, top: 2 });
  });

  it("follows pan and zoom through screen-space positioning and gives selected badges priority", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [annotation("ann-1"), annotation("ann-2")],
      counts: { "ann-1": 1, "ann-2": 1 },
      imgW: 100,
      imgH: 80,
      vp: view,
      selectedIds: new Set(["ann-2"]),
    });
    expect(models[0]).toMatchObject({ left: 118, top: 24, priority: 1 });
    expect(models[1]).toMatchObject({ left: 118, top: 24, priority: 2 });
    const moved = buildAnnotationCommentBadges({
      annotations: [annotation("ann-1")],
      counts: { "ann-1": 1 },
      imgW: 100,
      imgH: 80,
      vp: { scale: 3, tx: -5, ty: 7 },
    });
    expect(moved[0]).toMatchObject({ left: 143, top: 27 });
  });

  it("keeps the full 40px target away from the corner handle and reachable at an edge", () => {
    const models = buildAnnotationCommentBadges({
      annotations: [
        annotation("edge", {
          x: 0.05,
          w: 0.5,
          geometry: { type: "bbox", x: 0.05, y: 0.2, w: 0.5, h: 0.2 },
        }),
      ],
      counts: { edge: 1 },
      imgW: 100,
      imgH: 80,
      vp: view,
      viewportSize: { w: 120, h: 80 },
    });
    expect(models[0].left).toBeCloseTo(92);
    expect(models[0].top).toBe(24);
    expect(models[0].left + 20).toBeLessThanOrEqual(120);
    expect(models[0].top + 20).toBeLessThan(52);
  });

  it("provides an accessible button and isolates canvas pointer events", () => {
    const open = vi.fn();
    const parent = vi.fn();
    render(
      <div
        onPointerDown={parent}
        onClick={parent}
        onDoubleClick={parent}
        onKeyDown={parent}
        onKeyUp={parent}
      >
        <ImageStageCommentBadges
          annotations={[annotation("ann-1")]}
          counts={{ "ann-1": 10 }}
          imgW={100}
          imgH={80}
          vp={view}
          onOpenAnnotationComments={open}
        />
      </div>,
    );
    const badge = screen.getByRole("button", { name: "标注 ann-1 有 10 条评论" });
    expect(badge).toHaveAttribute("title", "标注 ann-1 有 10 条评论");
    expect(badge).toHaveAttribute("data-annotation-id", "ann-1");
    fireEvent.pointerDown(badge);
    fireEvent.pointerUp(badge);
    fireEvent.click(badge);
    fireEvent.doubleClick(badge);
    fireEvent.keyDown(badge, { key: "Enter" });
    fireEvent.keyUp(badge, { key: "Enter" });
    expect(open).toHaveBeenCalledOnce();
    expect(parent).not.toHaveBeenCalled();
  });

  it("keeps hover stacking in CSS while preserving selected priority", () => {
    render(
      <ImageStageCommentBadges
        annotations={[annotation("ann-1"), annotation("ann-2")]}
        counts={{ "ann-1": 1, "ann-2": 1 }}
        imgW={100}
        imgH={80}
        vp={view}
      />,
    );
    const badges = screen.getAllByTestId("annotation-comment-badge");
    expect(badges[0]).toHaveAttribute("data-priority", "1");
    fireEvent.mouseEnter(badges[1]);
    expect(badges[1]).toHaveAttribute("data-priority", "1");
    fireEvent.mouseLeave(badges[1]);
    expect(badges[1]).toHaveAttribute("data-priority", "1");
  });

  it("does not activate a focused target while interaction is disabled", () => {
    const open = vi.fn();
    render(
      <ImageStageCommentBadges
        annotations={[annotation("ann-1")]}
        counts={{ "ann-1": 1 }}
        imgW={100}
        imgH={80}
        vp={view}
        interactive={false}
        onOpenAnnotationComments={open}
      />,
    );
    const badge = screen.getByRole("button", { name: "标注 ann-1 有 1 条评论" });
    expect(badge).toBeDisabled();
    fireEvent.click(badge);
    fireEvent.keyDown(badge, { key: "Enter" });
    expect(open).not.toHaveBeenCalled();
  });
});
