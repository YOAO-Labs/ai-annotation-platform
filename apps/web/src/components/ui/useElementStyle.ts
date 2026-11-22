import { useCallback, useLayoutEffect, useRef, useState } from "react";
import type { CSSProperties, MutableRefObject, Ref } from "react";

const UNITLESS_NUMBER_PROPS = new Set([
  "animationIterationCount",
  "aspectRatio",
  "borderImageOutset",
  "borderImageSlice",
  "borderImageWidth",
  "boxFlex",
  "boxFlexGroup",
  "boxOrdinalGroup",
  "columnCount",
  "columns",
  "flex",
  "flexGrow",
  "flexNegative",
  "flexOrder",
  "flexPositive",
  "flexShrink",
  "fontWeight",
  "gridArea",
  "gridColumn",
  "gridColumnEnd",
  "gridColumnStart",
  "gridRow",
  "gridRowEnd",
  "gridRowStart",
  "lineClamp",
  "lineHeight",
  "opacity",
  "order",
  "orphans",
  "scale",
  "tabSize",
  "widows",
  "zIndex",
  "zoom",
]);

function toKebabCase(name: string) {
  return name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`);
}

function serializeStyleValue(name: string, value: string | number) {
  if (typeof value === "number" && value !== 0 && !UNITLESS_NUMBER_PROPS.has(name)) {
    return `${value}px`;
  }
  return String(value);
}

function setRef<T>(ref: Ref<T> | undefined, value: T | null) {
  if (!ref) return;
  if (typeof ref === "function") {
    ref(value);
  } else {
    (ref as MutableRefObject<T | null>).current = value;
  }
}

export function useElementStyle<T extends HTMLElement | SVGElement>(
  style: CSSProperties | undefined,
  forwardedRef?: Ref<T>,
) {
  const [element, setElement] = useState<T | null>(null);
  const appliedKeysRef = useRef<Set<string>>(new Set());

  const ref = useCallback(
    (el: T | null) => {
      setElement(el);
      setRef(forwardedRef, el);
    },
    [forwardedRef],
  );

  useLayoutEffect(() => {
    if (!element) return;

    const nextKeys = new Set(Object.keys(style ?? {}));
    for (const key of appliedKeysRef.current) {
      if (!nextKeys.has(key)) {
        element.style.removeProperty(key.startsWith("--") ? key : toKebabCase(key));
      }
    }

    if (style) {
      for (const [key, rawValue] of Object.entries(style)) {
        const value = rawValue as string | number | null | undefined;
        const propertyName = key.startsWith("--") ? key : toKebabCase(key);
        if (value == null || value === "") {
          element.style.removeProperty(propertyName);
        } else {
          element.style.setProperty(propertyName, serializeStyleValue(key, value));
        }
      }
    }

    appliedKeysRef.current = nextKeys;
  }, [style, element]);

  return ref;
}
