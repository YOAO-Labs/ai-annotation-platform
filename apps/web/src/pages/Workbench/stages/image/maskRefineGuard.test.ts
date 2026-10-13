import { describe, expect, it } from "vitest";
import type { AnnotationResponse } from "@/types";
import { useDecisionDialogStore } from "@/components/ui/decisionDialog";
import { maskRefineBlockReason, promptEmptyRasterMaskChoice } from "./useImageAnnotationActions";

/** 结算当前排队的 decisionDialog 并清空队列,模拟用户点下某个选项。 */
function settleDialog(value: boolean | string | null) {
  const head = useDecisionDialogStore.getState().queue[0];
  if (!head) throw new Error("decisionDialog 队列为空");
  useDecisionDialogStore.getState().settle(value);
  useDecisionDialogStore.getState().dispose();
}

function annotation(
  geometry: AnnotationResponse["geometry"],
  isLocked = false,
): AnnotationResponse {
  return { geometry, is_locked: isLocked } as AnnotationResponse;
}

describe("maskRefineBlockReason", () => {
  it("拒绝 task/annotation 锁定对象", () => {
    const polygon = annotation({
      type: "polygon",
      points: [
        [0, 0],
        [1, 0],
        [0, 1],
      ],
    });
    expect(maskRefineBlockReason(polygon, true)).toContain("锁定");
    expect(maskRefineBlockReason({ ...polygon, is_locked: true }, false)).toContain("锁定");
  });

  it("拒绝 multi_polygon 与带 holes 的复杂几何", () => {
    const multi = annotation({
      type: "multi_polygon",
      polygons: [
        {
          type: "polygon",
          points: [
            [0, 0],
            [1, 0],
            [0, 1],
          ],
          holes: [],
        },
      ],
    });
    const holes = annotation({
      type: "polygon",
      points: [
        [0, 0],
        [1, 0],
        [0, 1],
      ],
      holes: [
        [
          [0.2, 0.2],
          [0.3, 0.2],
          [0.2, 0.3],
        ],
      ],
    } as AnnotationResponse["geometry"]);
    expect(maskRefineBlockReason(multi, false)).toContain("复杂几何");
    expect(maskRefineBlockReason(holes, false)).toContain("复杂几何");
  });
});

describe("promptEmptyRasterMaskChoice", () => {
  it("三态选择依次映射删除、撤销和继续编辑", async () => {
    let choice = promptEmptyRasterMaskChoice();
    settleDialog("delete");
    await expect(choice).resolves.toBe("delete");
    choice = promptEmptyRasterMaskChoice();
    settleDialog("undo");
    await expect(choice).resolves.toBe("undo");
    // 取消 / Esc / 点遮罩 → null → 保持空白继续编辑
    choice = promptEmptyRasterMaskChoice();
    settleDialog(null);
    await expect(choice).resolves.toBe("continue");
  });
});
