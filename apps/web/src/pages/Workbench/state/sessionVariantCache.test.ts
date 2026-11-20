import { afterEach, describe, expect, it } from "vitest";
import {
  isVariantHot,
  markVariantHot,
  recordPredictCacheHit,
  _resetVariantHotCache,
} from "./sessionVariantCache";

describe("sessionVariantCache", () => {
  afterEach(() => {
    _resetVariantHotCache();
  });

  it("isVariantHot 默认为 false (空 cache)", () => {
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(false);
  });

  it("不同 variant 互不影响", () => {
    markVariantHot("b1", { series: "yolo11", size: "s" });
    expect(isVariantHot("b1", { series: "yolo11", size: "n" })).toBe(false);
    expect(isVariantHot("b1", { series: "yolov8", size: "s" })).toBe(false);
  });

  it("不同 backend 互不影响", () => {
    markVariantHot("b1", { series: "yolo11", size: "s" });
    expect(isVariantHot("b2", { series: "yolo11", size: "s" })).toBe(false);
  });

  it("backendId 为 null/undefined 时是 noop", () => {
    markVariantHot(null, { series: "yolo11", size: "s" });
    markVariantHot(undefined, { series: "yolo11", size: "s" });
    expect(isVariantHot(null, { series: "yolo11", size: "s" })).toBe(false);
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(false);
  });

  it("axis_key 顺序无关 (mark 用 a/b vs is 用 b/a)", () => {
    markVariantHot("b1", { series: "yolo11", size: "s" });
    expect(isVariantHot("b1", { size: "s", series: "yolo11" })).toBe(true);
  });

  it("非 string value 被忽略 (不污染 key)", () => {
    markVariantHot("b1", { series: "yolo11", count: 42, weight: null });
    expect(isVariantHot("b1", { series: "yolo11" })).toBe(true);
  });
});

describe("recordPredictCacheHit (真信号路径)", () => {
  afterEach(() => {
    _resetVariantHotCache();
  });

  it("record(true) 后 isVariantHot 返回 true", () => {
    recordPredictCacheHit("b1", { series: "yolo11", size: "s" }, true);
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(true);
  });

  it("record(false) 后 isVariantHot 返回 false (协议 §4.2 真信号: 本次冷启动)", () => {
    recordPredictCacheHit("b1", { series: "yolo11", size: "s" }, false);
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(false);
  });

  it("evict 后真信号降级: markHot → record(false) 让 isVariantHot 返回 false", () => {
    markVariantHot("b1", { series: "yolo11", size: "s" });
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(true);
    recordPredictCacheHit("b1", { series: "yolo11", size: "s" }, false);
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(false);
  });

  it("cache_hit=null/undefined 时不污染 Map (协议 §4.2 字段缺省)", () => {
    markVariantHot("b1", { series: "yolo11", size: "s" });
    recordPredictCacheHit("b1", { series: "yolo11", size: "s" }, null);
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(true);
    recordPredictCacheHit("b1", { series: "yolo11", size: "s" }, undefined);
    expect(isVariantHot("b1", { series: "yolo11", size: "s" })).toBe(true);
  });

  it("backendId 为 null/undefined 时是 noop", () => {
    recordPredictCacheHit(null, { series: "yolo11", size: "s" }, true);
    recordPredictCacheHit(undefined, { series: "yolo11", size: "s" }, true);
    expect(isVariantHot(null, { series: "yolo11", size: "s" })).toBe(false);
  });
});
