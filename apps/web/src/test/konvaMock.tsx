import * as React from "react";

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
  onTap: "onClick",
  onWheel: "onWheel",
};

const DRAG_EVENT_PROPS = ["onDragStart", "onDragMove", "onDragEnd"] as const;

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
  "fillRule",
] as const;

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

function makeKonvaStandIn(konvaType: string) {
  const Component = React.forwardRef<HTMLDivElement, AnyProps>((props, ref) => {
    const { children, name, ...rest } = props;
    const domProps: Record<string, unknown> = {
      "data-konva": konvaType,
      ref,
    };
    if (name != null) domProps["data-testid"] = name;

    for (const key of PASSTHROUGH_PROPS) {
      const v = rest[key];
      if (v === undefined) continue;
      const dataKey = `data-${key.toLowerCase()}`;
      domProps[dataKey] = Array.isArray(v) || typeof v === "object" ? JSON.stringify(v) : String(v);
    }

    for (const [konvaProp, domProp] of Object.entries(EVENT_PROP_MAP)) {
      const handler = rest[konvaProp];
      if (typeof handler !== "function") continue;
      domProps[domProp] = (e: React.SyntheticEvent) => {
        const node = (e.currentTarget ?? e.target) as HTMLElement;
        (handler as (ev: ReturnType<typeof toKonvaEvent>) => void)(toKonvaEvent(e, node));
      };
    }

    for (const dragProp of DRAG_EVENT_PROPS) {
      if (typeof rest[dragProp] === "function") {
        domProps[`data-has-${dragProp.toLowerCase()}`] = "true";
      }
    }

    const textContent = typeof rest.text === "string" ? rest.text : null;
    return React.createElement("div", domProps, children as React.ReactNode, textContent);
  });
  Component.displayName = `KonvaMock(${konvaType})`;
  return Component;
}

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
