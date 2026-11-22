import assert from "node:assert/strict";
import { test } from "vitest";

import {
  invalidSeekTarget,
  rangeValueForFrame,
  timelineClickX,
} from "../e2e/helpers/video-timeline-seek";

const FULL = { from: 0, to: 179 };

test("180 帧全窗口逐帧可经 range 归一化精确表达", () => {
  for (let frame = 0; frame <= 179; frame += 1) {
    const mapping = rangeValueForFrame(frame, FULL);
    assert.equal(mapping.expressible, true, `F${frame}`);
    assert.equal(mapping.representedFrame, frame, `F${frame}`);
    assert.ok(mapping.value >= 0 && mapping.value <= 10000, `F${frame} value`);
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
  for (const shellWidth of [360, width, 1200]) {
    assert.equal(timelineClickX(0, FULL, shellWidth), 2);
    assert.equal(timelineClickX(179, FULL, shellWidth), shellWidth - 2);
  }
  assert.ok(Math.abs(timelineClickX(120, FULL, width) - (120 / 179) * width) < 1e-9);
  assert.throws(() => timelineClickX(90, FULL, 0), /宽度不可用/);
  assert.throws(() => timelineClickX(90, { from: 5, to: 5 }, width), /退化/);
});
