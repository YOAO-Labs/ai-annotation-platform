/**
 * react-konva DOM stand-in mock(画布栈统一地基 · 决策 C 的工程落地)。
 *
 * ⚠️ 本 mock 只验证「交互 + props 透传」,不验证真实 canvas 渲染。
 *     真实渲染回归交给 Playwright(见 e2e/tests/workbench-image-konva-smoke.spec.ts)。
 *     —— 三层测试分工:纯函数单测 / konva mock(本文件)/ Playwright 截图基线。
 *
 * 做法:把每个 Konva 组件渲染成
 *     <div data-konva="Rect" data-testid={props.name} ...几何/样式 props 透传 ...>
 *   - 几何/样式 props(x/y/width/height/points/radius/fill/stroke/strokeWidth/dash/
 *     scaleX/scaleY/opacity/rotation/cornerRadius/offsetX/offsetY 等)以 data-* 透传,
 *     字符串/数字直接写,数组(points/dash)JSON.stringify;
 *   - 事件 props(onMouseDown/onMouseUp/onClick/onPointerDown/onPointerMove/
 *     onPointerUp/onTap/onDragStart/onDragMove/onDragEnd/onWheel)挂到 DOM 上,
 *     使 RTL 的 fireEvent 可触发;回调收到近似 Konva 事件形态的对象
 *     (含 evt / target / cancelBubble),让被测组件读 e.evt.clientX / e.cancelBubble 不炸;
 *   - children 正常渲染(Stage/Layer/Group/Label 是容器)。
 *
 * 用法:在 vitest.setup.ts 顶层
 *     vi.mock("react-konva", () => import("./src/test/konvaMock"));
 * (vi.mock 提升语义 + 工厂返回 Promise,vitest 会 await 该模块。)
 */
import * as React from "react";

/** 事件 prop 名 → 触发它的 DOM 事件 prop 名(React 合成事件)。 */
const EVENT_PROP_MAP: Record<string, string> = {
  onMouseDown: "onMouseDown",
  onMouseUp: "onMouseUp",
  onMouseEnter: "onMouseEnter",
  onMouseLeave: "onMouseLeave",
  onMouseMove: "onMouseMove",
  onClick: "onClick",
  onDblClick: "onDoubleClick",
  onPointerDown: "onPointerDown",
  onPointerMove: "onPointerMove",
  onPointerUp: "onPointerUp",
  onTap: "onClick", // Konva onTap ≈ 触屏点击,mock 里映射到 click 便于 fireEvent
  onWheel: "onWheel",
  // drag 事件无对应原生 DOM 事件,挂成自定义 data-has-* 标记(见下),不直接转发
};

/** 拖拽类事件:DOM 无原生对应,仅记录「组件声明了该回调」,测试可手动调用 props。 */
const DRAG_EVENT_PROPS = ["onDragStart", "onDragMove", "onDragEnd"] as const;

/** 几何/样式 props:透传成 data-* 供断言。值为数组时 JSON 序列化。 */
const PASSTHROUGH_PROPS = [
  "x",
  "y",
  "width",
  "height",
  "radius",
  "rotation",
  "scaleX",
  "scaleY",
  "offsetX",
  "offsetY",
  "fill",
  "stroke",
  "strokeWidth",
  "opacity",
  "cornerRadius",
  "points",
  "dash",
  "closed",
  "text",
  "align",
  "verticalAlign",
  "fontSize",
  "fontFamily",
  "padding",
  "lineCap",
  "lineJoin",
  "hitStrokeWidth",
  "shadowColor",
  "shadowBlur",
  "shadowOpacity",
  "shadowEnabled",
  "listening",
  "id",
  "visible",
  // fillRule=evenodd 标记 KonvaPolygon 走 sceneFunc holes/multi 分支,
  // sceneFunc 本身是函数无法 JSON 化, 但 fillRule 的存在性可断言分支是否启用。
  "fillRule",
] as const;

/** 构造一个近似 Konva 事件对象:含 evt(原始 DOM 事件)/ target / cancelBubble。 */
function toKonvaEvent(domEvent: React.SyntheticEvent, node: HTMLElement) {
  const fakeNode = {
    getStage: () => ({
      container: () => ({ style: {} as Record<string, string> }),
    }),
  };
  return {
    evt: domEvent.nativeEvent ?? domEvent,
    target: fakeNode,
    currentTarget: fakeNode,
    cancelBubble: false,
    type: domEvent.type,
    _node: node,
  };
}

type AnyProps = Record<string, unknown> & { children?: React.ReactNode; name?: string };

/** 工厂:生成一个把 props 映射到 data-* + 事件转发的 div stand-in 组件。 */
function makeKonvaStandIn(konvaType: string) {
  const Component = React.forwardRef<HTMLDivElement, AnyProps>((props, ref) => {
    const { children, name, ...rest } = props;
    const domProps: Record<string, unknown> = {
      "data-konva": konvaType,
      ref,
    };
    if (name != null) domProps["data-testid"] = name;

    // 几何/样式 props → data-*
    for (const key of PASSTHROUGH_PROPS) {
      const v = rest[key];
      if (v === undefined) continue;
      const dataKey = `data-${key.toLowerCase()}`;
      domProps[dataKey] = Array.isArray(v) || typeof v === "object" ? JSON.stringify(v) : String(v);
    }

    // 事件 props → DOM 事件,回调包一层近似 Konva 事件对象
    for (const [konvaProp, domProp] of Object.entries(EVENT_PROP_MAP)) {
      const handler = rest[konvaProp];
      if (typeof handler !== "function") continue;
      domProps[domProp] = (e: React.SyntheticEvent) => {
        const node = (e.currentTarget ?? e.target) as HTMLElement;
        (handler as (ev: ReturnType<typeof toKonvaEvent>) => void)(toKonvaEvent(e, node));
      };
    }

    // 拖拽回调:仅标记声明,测试可读 data-has-ondragmove 或直接调 props
    for (const dragProp of DRAG_EVENT_PROPS) {
      if (typeof rest[dragProp] === "function") {
        domProps[`data-has-${dragProp.toLowerCase()}`] = "true";
      }
    }

    // Konva Text/Label 的文字经 `text` prop 传入(无 children)。除透传 data-text 外,
    // 同时渲染成 DOM 文本内容,使测试可用 RTL 习惯的 getByText 断言标签文字。
    const textContent = typeof rest.text === "string" ? rest.text : null;
    return React.createElement("div", domProps, children as React.ReactNode, textContent);
  });
  Component.displayName = `KonvaMock(${konvaType})`;
  return Component;
}

// 覆盖视频 / 图片用到的组件全集(按需可继续补)。
export const Stage = makeKonvaStandIn("Stage");
export const Layer = makeKonvaStandIn("Layer");
export const Group = makeKonvaStandIn("Group");
export const Rect = makeKonvaStandIn("Rect");
export const Line = makeKonvaStandIn("Line");
export const Circle = makeKonvaStandIn("Circle");
export const Label = makeKonvaStandIn("Label");
export const Tag = makeKonvaStandIn("Tag");
export const Text = makeKonvaStandIn("Text");
export const Image = makeKonvaStandIn("Image");
export const Path = makeKonvaStandIn("Path");
export const Arrow = makeKonvaStandIn("Arrow");
export const Ellipse = makeKonvaStandIn("Ellipse");
export const RegularPolygon = makeKonvaStandIn("RegularPolygon");
