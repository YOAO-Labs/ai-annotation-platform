import assert from "node:assert/strict";
import { test } from "vitest";

import {
  frameAtClickX,
  invalidSeekTarget,
  rangeValueForFrame,
  timelineClickX,
} from "../e2e/helpers/video-timeline-seek";

// h264-issue-context E2E 夹具：180 帧（0..179），与 apps/api
// _test_seed_webcodecs.py 的 FIXTURE_SPECS 保持一致。
const FULL = { from: 0, to: 179 };

test("180 帧全窗口逐帧可经 range 归一化精确表达", () => {
  for (let frame = 0; frame <= 179; frame += 1) {
    const mapping = rangeValueForFrame(frame, FULL);
    assert.equal(mapping.expressible, true, `F${frame}`);
    assert.equal(mapping.representedFrame, frame, `F${frame}`);
    assert.ok(mapping.value >= 0 && mapping.value <= 10000, `F${frame} value`);
  }
});

test("G2 套件实际使用的目标帧全部可表达", () => {
  for (const frame of [3, 17, 33, 45, 63, 93, 120, 121, 129, 140, 143, 159, 173]) {
    const mapping = rangeValueForFrame(frame, FULL);
    assert.equal(mapping.expressible, true, `F${frame}`);
  }
});

test("窗口偏移与缩放产生的分数端点不破坏精确表达", () => {
  for (const win of [
    { from: 10, to: 120 },
    { from: 120, to: 179 },
    { from: 44.75, to: 134.25 },
    { from: 0.5, to: 60.5 },
    { from: 30.25, to: 179 },
  ]) {
    for (let frame = Math.ceil(win.from); frame <= Math.floor(win.to); frame += 1) {
      const mapping = rangeValueForFrame(frame, win);
      assert.equal(mapping.expressible, true, `F${frame} in [${win.from}, ${win.to}]`);
      assert.equal(mapping.representedFrame, frame, `F${frame} in [${win.from}, ${win.to}]`);
    }
  }
});

test("超出 range 粒度的长窗口显式报告不可表达，不静默近似", () => {
  // span=12000 时一个归一化步长 ≈ 1.2 帧：F3 反解为 F4，必须拒绝。
  const mapping = rangeValueForFrame(3, { from: 0, to: 12000 });
  assert.equal(mapping.expressible, false);
  assert.equal(mapping.representedFrame, 4);
});

test("窗口退化与非法目标被拒绝", () => {
  assert.equal(rangeValueForFrame(3, { from: 5, to: 5 }).expressible, false);
  assert.equal(rangeValueForFrame(3, { from: 10, to: 5 }).expressible, false);
  for (const frame of [-1, 0.5, Number.NaN, Number.POSITIVE_INFINITY]) {
    assert.match(invalidSeekTarget(frame) ?? "", /非负整数/);
  }
  assert.equal(invalidSeekTarget(0), null);
  assert.equal(invalidSeekTarget(179), null);
});

test("窗口外目标不可表达，避免静默钳制到端点帧", () => {
  assert.equal(rangeValueForFrame(200, FULL).expressible, false);
  assert.equal(rangeValueForFrame(-1, FULL).expressible, false);
});

test("点击横坐标在端点内缩、中间帧按精确比例", () => {
  const width = 800;
  assert.equal(timelineClickX(0, FULL, width), 2);
  assert.equal(timelineClickX(179, FULL, width), width - 2);
  assert.ok(Math.abs(timelineClickX(120, FULL, width) - (120 / 179) * width) < 1e-9);
  assert.throws(() => timelineClickX(90, FULL, 0), /宽度不可用/);
  assert.throws(() => timelineClickX(90, { from: 5, to: 5 }, width), /退化/);
});

test("端点内缩位置经组件同款公式仍映射到端点帧", () => {
  // frameAtClickX 复刻 pctToFrame（timelineCoords.ts）对实际落点像素的映射。
  // 2px 内缩在 w>716 的 shell 上仍取整到端点帧；e2e 展开态宽度约 722。
  for (const width of [780, 900, 1200]) {
    assert.equal(frameAtClickX(timelineClickX(0, FULL, width), FULL, width), 0, `width=${width}`);
    assert.equal(
      frameAtClickX(timelineClickX(179, FULL, width), FULL, width),
      179,
      `width=${width}`,
    );
    assert.equal(frameAtClickX(timelineClickX(90, FULL, width), FULL, width), 90, `width=${width}`);
  }
});

test("落点像素量化后断言使用实际映射帧而非想当然目标", () => {
  // 窄 shell 或端点附近的比例漂移会改变取整结果（如 w=360 时 x=2 映射 F1）：
  // 预测函数必须如实报告实际映射帧，断言随之使用预测值。
  for (const width of [360, 722, 800]) {
    for (const frame of [0, 90, 179]) {
      const x = timelineClickX(frame, FULL, width);
      assert.equal(
        frameAtClickX(x, FULL, width),
        Math.max(0, Math.min(179, Math.round((x / width) * 179))),
        `F${frame} @w=${width}`,
      );
    }
  }
});

test("落点像素量化后断言使用实际映射帧而非想当然目标", () => {
  // 右端内缩 2px 在窄 shell 上量化后可能取整到前一帧：预测函数必须如实报告。
  const width = 360;
  const x = timelineClickX(179, FULL, width);
  assert.equal(frameAtClickX(x, FULL, width), Math.round((x / width) * 179));
});
