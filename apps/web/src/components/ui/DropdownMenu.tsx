import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type MutableRefObject,
  type ReactNode,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

import { Icon, type IconName } from "./Icon";

export interface DropdownItem {
  id: string;
  label: ReactNode;
  icon?: IconName;
  kbd?: string;
  active?: boolean;
  onSelect?: () => void;
  divider?: boolean;
  disabled?: boolean;
}

interface DropdownMenuBaseProps {
  trigger: (ctx: {
    open: boolean;
    toggle: () => void;
    close: () => void;
    ref: React.Ref<HTMLButtonElement>;
  }) => ReactNode;
  align?: "start" | "end";
  hostStyle?: CSSProperties;
  minWidth?: number;
  zIndex?: number;
  panelAriaLabel?: string;
  panelStyle?: CSSProperties;
  disablePanelPadding?: boolean;
}

interface DropdownMenuItemsProps extends DropdownMenuBaseProps {
  items: DropdownItem[];
  content?: never;
  footer?: ReactNode;
}

interface DropdownMenuContentProps extends DropdownMenuBaseProps {
  items?: never;
  content: (ctx: { close: () => void }) => ReactNode;
  footer?: never;
}

type DropdownMenuProps = DropdownMenuItemsProps | DropdownMenuContentProps;

const UNITLESS_STYLE_PROPS = new Set([
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
  "flexPositive",
  "flexShrink",
  "flexNegative",
  "flexOrder",
  "gridArea",
  "gridRow",
  "gridRowEnd",
  "gridRowSpan",
  "gridRowStart",
  "gridColumn",
  "gridColumnEnd",
  "gridColumnSpan",
  "gridColumnStart",
  "fontWeight",
  "lineClamp",
  "lineHeight",
  "opacity",
  "order",
  "orphans",
  "tabSize",
  "widows",
  "zIndex",
  "zoom",
]);

function cssPropertyName(key: string) {
  if (key.startsWith("--")) return key;
  return key.replace(/[A-Z]/g, (match) => `-${match.toLowerCase()}`);
}

function cssPropertyValue(key: string, value: string | number) {
  if (typeof value === "number" && value !== 0 && !UNITLESS_STYLE_PROPS.has(key)) {
    return `${value}px`;
  }
  return String(value);
}

function syncDomStyles(
  node: HTMLElement | null,
  next: CSSProperties | undefined,
  prev: MutableRefObject<CSSProperties | undefined>,
) {
  if (!node) return;
  for (const key of Object.keys(prev.current ?? {})) {
    if (!next || !(key in next)) node.style.removeProperty(cssPropertyName(key));
  }
  for (const [key, value] of Object.entries(next ?? {})) {
    const name = cssPropertyName(key);
    if (value === undefined || value === null) node.style.removeProperty(name);
    else node.style.setProperty(name, cssPropertyValue(key, value as string | number));
  }
  prev.current = next;
}

export function DropdownMenu(props: DropdownMenuProps) {
  const {
    trigger,
    align = "end",
    hostStyle,
    minWidth = 180,
    zIndex = 30,
    panelAriaLabel,
    panelStyle,
    disablePanelPadding,
  } = props;
  const items = "items" in props ? props.items : undefined;
  const content = "content" in props ? props.content : undefined;
  const footer = "footer" in props ? props.footer : undefined;

  const [open, setOpen] = useState(false);
  const hostRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const itemRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const prevHostStyleRef = useRef<CSSProperties | undefined>(undefined);
  const prevPanelStyleRef = useRef<CSSProperties | undefined>(undefined);
  const [focusId, setFocusId] = useState<string | null>(null);

  const selectableIdx = items
    ? items.map((it, i) => (!it.divider && !it.disabled ? i : -1)).filter((i) => i >= 0)
    : [];

  const focusIdx =
    items && focusId
      ? items.findIndex((it) => it.id === focusId && !it.divider && !it.disabled)
      : -1;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!hostRef.current?.contains(target) && !menuRef.current?.contains(target)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!items) return;
    if (!open) {
      setFocusId(null);
      return;
    }
    const active =
      items.find((it) => it.active && !it.divider && !it.disabled) ??
      items.find((it) => !it.divider && !it.disabled);
    setFocusId(active?.id ?? null);
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const moveFocus = (dir: 1 | -1) => {
    if (!items || selectableIdx.length === 0) return;
    const cur = selectableIdx.indexOf(focusIdx);
    const next = cur < 0 ? 0 : (cur + dir + selectableIdx.length) % selectableIdx.length;
    setFocusId(items[selectableIdx[next]]?.id ?? null);
  };

  useEffect(() => {
    if (!open || !items) return;
    const target = focusIdx >= 0 ? itemRefs.current[focusIdx] : menuRef.current;
    target?.focus();
  }, [focusIdx, open]); // eslint-disable-line react-hooks/exhaustive-deps

  const onMenuKey = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!items) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      moveFocus(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      moveFocus(-1);
    } else if (e.key === "Home") {
      e.preventDefault();
      setFocusId(items[selectableIdx[0] ?? -1]?.id ?? null);
    } else if (e.key === "End") {
      e.preventDefault();
      setFocusId(items[selectableIdx[selectableIdx.length - 1] ?? -1]?.id ?? null);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      const it = items[focusIdx];
      if (it && !it.divider && !it.disabled) {
        it.onSelect?.();
        setOpen(false);
        triggerRef.current?.focus();
      }
    } else if (e.key === "Tab") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
    }
  };

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  useLayoutEffect(() => {
    syncDomStyles(hostRef.current, hostStyle, prevHostStyleRef);
  }, [hostStyle]);

  useLayoutEffect(() => {
    const menu = menuRef.current;
    if (!menu) return;
    menu.style.setProperty("--dropdown-min-width", `${minWidth}px`);
    menu.style.setProperty("--dropdown-z-index", String(zIndex));
    menu.style.setProperty("--dropdown-padding", disablePanelPadding ? "0" : "4px");
    syncDomStyles(menu, panelStyle, prevPanelStyleRef);
  }, [disablePanelPadding, minWidth, panelStyle, zIndex, open]);

  useLayoutEffect(() => {
    if (!open) return;

    const updatePosition = () => {
      const triggerNode = triggerRef.current;
      const menuNode = menuRef.current;
      if (!triggerNode || !menuNode) return;

      const triggerRect = triggerNode.getBoundingClientRect();
      const menuWidth = menuNode.offsetWidth || minWidth;
      const menuHeight = menuNode.offsetHeight;
      const margin = 8;
      const gap = 4;

      let left = align === "start" ? triggerRect.left : triggerRect.right - menuWidth;
      left = Math.max(margin, Math.min(left, window.innerWidth - margin - menuWidth));

      let top = triggerRect.bottom + gap;
      const flippedTop = triggerRect.top - gap - menuHeight;
      if (
        menuHeight > 0 &&
        top + menuHeight > window.innerHeight - margin &&
        flippedTop >= margin
      ) {
        top = flippedTop;
      }
      if (menuHeight > 0) {
        top = Math.min(top, window.innerHeight - margin - menuHeight);
      }
      top = Math.max(margin, top);

      menuNode.style.setProperty("--dropdown-left", `${left}px`);
      menuNode.style.setProperty("--dropdown-top", `${top}px`);
    };

    updatePosition();
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);
    return () => {
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [align, minWidth, open]);

  const menuPanel = open ? (
    <div
      ref={menuRef}
      role={items ? "menu" : "dialog"}
      aria-label={panelAriaLabel}
      aria-orientation={items ? "vertical" : undefined}
      tabIndex={-1}
      onKeyDown={onMenuKey}
      className="fixed left-[var(--dropdown-left)] top-[var(--dropdown-top)] z-[var(--dropdown-z-index)] min-w-[var(--dropdown-min-width)] rounded-md border border-border bg-popover p-[var(--dropdown-padding)] shadow-md outline-none"
    >
      {content
        ? content({ close })
        : items?.map((it, i) => {
            if (it.divider) {
              return (
                <div key={it.id || `div-${i}`} role="separator" className="my-1 h-px bg-border" />
              );
            }
            const focused = it.id === focusId;
            return (
              <button
                key={it.id}
                ref={(el) => {
                  itemRefs.current[i] = el;
                }}
                type="button"
                role="menuitem"
                tabIndex={focused ? 0 : -1}
                disabled={it.disabled}
                onClick={() => {
                  if (it.disabled) return;
                  it.onSelect?.();
                  setOpen(false);
                  triggerRef.current?.focus();
                }}
                onMouseEnter={() => {
                  if (it.disabled) return;
                  setFocusId(it.id);
                }}
                className={cn(
                  "flex w-full appearance-none items-center gap-2 rounded-sm border-0 bg-transparent px-2.5 py-2 text-left text-sm font-normal text-muted-foreground",
                  (it.active || focused) && "bg-accent",
                  it.active && "font-semibold text-foreground",
                  it.disabled && "cursor-not-allowed text-muted-foreground/60 opacity-60",
                )}
              >
                {it.icon && <Icon name={it.icon} size={13} />}
                <span className="flex-1">{it.label}</span>
                {it.kbd && (
                  <span className="mono rounded border border-b-2 border-border bg-muted px-1.5 py-px text-2xs text-muted-foreground">
                    {it.kbd}
                  </span>
                )}
                {it.active && !it.kbd && <Icon name="check" size={12} className="text-brand" />}
              </button>
            );
          })}
      {footer}
    </div>
  ) : null;

  return (
    <div ref={hostRef} className="inline-flex">
      {trigger({
        open,
        toggle: () => {
          if (open) triggerRef.current?.focus();
          setOpen(!open);
        },
        close,
        ref: triggerRef,
      })}
      {menuPanel && createPortal(menuPanel, document.body)}
    </div>
  );
}
