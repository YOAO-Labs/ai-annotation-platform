import {
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { ChevronDown, Ellipsis } from "lucide-react";
import {
  Popover,
  PopoverAnchor,
  PopoverContent,
  PopoverTrigger,
} from "@/components/shadcn/ui/popover";
import { cn } from "@/lib/utils";
import styles from "./ContextToolbar.module.css";

export interface ContextToolbarAction {
  id: string;
  label: string;
  shortLabel?: string;
  icon: ReactNode;
  onSelect: () => void;
  active?: boolean;
  disabled?: boolean;
}

interface ContextToolbarProps {
  id: string;
  label: string;
  summary: ReactNode;
  summaryLabel: string;
  summaryTitle?: string;
  quickActions: readonly ContextToolbarAction[];
  /** Frequent input and decisions appear in the hover disclosure, before full settings. */
  primaryContent?: ReactNode;
  panelSize?: "compact" | "wide";
  children: (close: () => void) => ReactNode;
}

/**
 * A stacked modal decision dialog owns pointer and focus interactions that land
 * inside it. Radix's outside detection can otherwise retire this toolbar
 * mid-flow: when the dialog closes between the interaction and the deferred
 * outside check, the interaction is misattributed as an outside click and the
 * expanded panel collapses (observed as the tracker review bar vanishing right
 * after a manual-keyframe override confirm on slow machines).
 */
function isWithinModalDialog(target: EventTarget | null): boolean {
  return (
    target instanceof Element &&
    !!target.closest('[data-modal], [role="dialog"], [role="alertdialog"]')
  );
}

/** Presentation only: drafts, settings, commands and persistence stay with the tool owner. */
export function ContextToolbar({
  id,
  label,
  summary,
  summaryLabel,
  summaryTitle,
  quickActions,
  primaryContent,
  panelSize = "wide",
  children,
}: ContextToolbarProps) {
  const [open, setOpen] = useState(false);
  const [quickOpen, setQuickOpen] = useState(false);
  const summaryRef = useRef<HTMLButtonElement>(null);
  const summaryContentRef = useRef<HTMLSpanElement>(null);
  const [compactWidth, setCompactWidth] = useState<number>();
  useLayoutEffect(() => {
    const content = summaryContentRef.current;
    if (!content) return;
    const measure = () => {
      const width = content.getBoundingClientRect().width;
      if (width > 0) setCompactWidth(Math.min(240, Math.ceil(width) + 26));
    };
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(content);
    return () => observer.disconnect();
  }, []);
  const interactedOutsideRef = useRef(false);
  const capsuleRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const motionFrameRef = useRef(0);
  const [boundary, setBoundary] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setBoundary(capsuleRef.current?.offsetParent as HTMLElement | null);
  }, []);
  useEffect(() => () => cancelAnimationFrame(motionFrameRef.current), []);
  const sourceRef = useRef<DOMRect | null>(null);

  function prepareMotion(opening: boolean) {
    const panel = panelRef.current;
    const source = opening ? sourceRef.current : capsuleRef.current?.getBoundingClientRect();
    // The Popper wrapper stays at the final position while its child animates.
    const target = panel?.parentElement?.getBoundingClientRect();
    if (!panel || !source || !target || !target.width || !target.height) return;
    const sourceWidth =
      !opening && compactWidth != null ? Math.min(compactWidth, source.width) : source.width;
    const capsuleTransform = `translate(${source.x - target.x}px, ${source.y - target.y}px) scale(${sourceWidth / target.width}, ${source.height / target.height})`;
    const interrupted = panel.getAttribute("data-motion-ready") === "true";
    const current = getComputedStyle(panel);
    const from = interrupted ? current.transform : capsuleTransform;
    const opacity = interrupted ? current.opacity : "0.25";
    const radius = interrupted ? current.borderRadius : "999px";
    panel.style.setProperty("--toolbar-from", from);
    panel.style.setProperty("--toolbar-from-opacity", opacity);
    panel.style.setProperty("--toolbar-from-radius", radius);
    panel.style.setProperty("--toolbar-target", capsuleTransform);
    panel.setAttribute("data-motion-ready", "true");
  }

  function changeOpen(next: boolean) {
    cancelAnimationFrame(motionFrameRef.current);
    if (next) {
      sourceRef.current = capsuleRef.current?.getBoundingClientRect() ?? null;
      setBoundary(capsuleRef.current?.offsetParent as HTMLElement | null);
      interactedOutsideRef.current = false;
    }
    prepareMotion(next);
    setOpen(next);
  }

  return (
    <Popover open={open} onOpenChange={changeOpen} modal={false}>
      <div
        ref={capsuleRef}
        data-testid={`${id}-tool-capsule`}
        data-workbench-context-quick-tools={quickOpen ? "" : undefined}
        data-expanded={quickOpen}
        data-panel-open={open}
        aria-hidden={open}
        // eslint-disable-next-line no-restricted-syntax -- Intrinsic text measurement supplies a dynamic CSS width variable.
        style={
          {
            "--toolbar-compact-width": compactWidth == null ? undefined : `${compactWidth}px`,
          } as CSSProperties
        }
        className={cn(
          "absolute left-3 top-3 z-local-5 h-9 max-w-[calc(100%-1.5rem)]",
          styles.capsule,
        )}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
          if (event.key === "Escape" && quickOpen) {
            event.preventDefault();
            event.stopPropagation();
            setQuickOpen(false);
          }
        }}
        onPointerEnter={(event) => {
          if (event.buttons === 0) setQuickOpen(true);
        }}
        onPointerLeave={(event) => {
          if (!event.currentTarget.querySelector(":focus-visible")) setQuickOpen(false);
        }}
        onFocusCapture={() => setQuickOpen(true)}
        onBlurCapture={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setQuickOpen(false);
        }}
        onPointerDown={(event) => event.stopPropagation()}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="overflow-hidden rounded-[1.125rem] border border-border/60 bg-card/95 shadow-sm backdrop-blur-md">
          <div className="flex h-[34px] min-w-0 items-center">
            <button
              ref={summaryRef}
              type="button"
              data-workbench-context-toolbar-trigger
              data-testid={`${id}-settings-trigger`}
              aria-label={summaryLabel}
              aria-expanded={quickOpen}
              title={summaryTitle}
              className="flex h-full w-full min-w-0 items-center gap-2 overflow-hidden whitespace-nowrap rounded-full px-3 text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => setQuickOpen(true)}
            >
              <span ref={summaryContentRef} className="flex w-max shrink-0 items-center gap-2">
                {summary}
              </span>
              <ChevronDown
                aria-hidden
                className={cn(
                  "absolute right-3 size-3.5 text-muted-foreground transition-[transform,opacity] duration-160 motion-reduce:transition-none",
                  quickOpen ? "rotate-180 opacity-100" : "opacity-0",
                )}
              />
            </button>
          </div>
          <div
            data-testid={`${id}-quick-disclosure`}
            aria-hidden={!quickOpen}
            {...(!quickOpen ? { inert: "" } : {})}
            className={cn(styles.quickDisclosure, quickOpen && styles.quickDisclosureOpen)}
          >
            <div className="min-h-0 overflow-hidden">
              <div
                className={cn(
                  "flex max-h-[min(24rem,60vh)] flex-col gap-2 overflow-y-auto px-2 pb-2 pt-1",
                  styles.quickContent,
                )}
              >
                <div
                  data-testid={`${id}-quick-tools`}
                  aria-hidden={!quickOpen}
                  className="flex flex-col gap-0.5"
                >
                  {quickActions.map((action) => (
                    <button
                      key={action.id}
                      type="button"
                      aria-label={action.label}
                      title={action.label}
                      aria-pressed={action.active}
                      disabled={action.disabled}
                      tabIndex={quickOpen ? 0 : -1}
                      className={cn(
                        "relative flex h-7 w-full shrink-0 items-center gap-2 rounded-lg px-2 text-xs outline-none transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40 motion-reduce:transition-none",
                        action.active
                          ? "bg-primary/10 text-primary"
                          : "text-muted-foreground hover:text-foreground",
                      )}
                      onClick={action.onSelect}
                    >
                      <span className="flex size-4 shrink-0 items-center justify-center">
                        {action.icon}
                      </span>
                      <span className="min-w-0 truncate">{action.shortLabel ?? action.label}</span>
                    </button>
                  ))}
                </div>
                {!open && primaryContent && (
                  <div
                    data-workbench-context-primary
                    className="flex min-w-0 flex-wrap items-center gap-1.5 [&_input]:max-w-full [&_select]:max-w-full"
                  >
                    {primaryContent}
                  </div>
                )}
                <PopoverTrigger
                  aria-label={`更多 ${label} 工具`}
                  title="更多工具与设置"
                  tabIndex={quickOpen ? 0 : -1}
                  className="flex h-7 w-full shrink-0 items-center gap-2 rounded-lg px-2 text-xs text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                >
                  <Ellipsis className="size-4" />
                  <span>更多设置</span>
                </PopoverTrigger>
              </div>
            </div>
          </div>
        </div>
      </div>
      {/* Mount after the trigger so Radix retains the canvas anchor on first render. */}
      <PopoverAnchor asChild>
        <div className="pointer-events-none absolute left-1/2 top-3 h-0 w-0" />
      </PopoverAnchor>
      <PopoverContent
        asChild
        align="center"
        sideOffset={0}
        collisionPadding={12}
        collisionBoundary={boundary}
        className={cn(
          "z-workbench-top flex w-[min(42rem,calc(100vw-1.5rem))] max-w-[var(--radix-popover-content-available-width)] max-h-[var(--radix-popover-content-available-height)] flex-col gap-3 overflow-y-auto rounded-2xl border border-border bg-card px-3 py-3 shadow-lg",
          panelSize === "compact" && "w-[min(28rem,calc(100vw-1.5rem))]",
          styles.panel,
        )}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          motionFrameRef.current = requestAnimationFrame(() => {
            if (panelRef.current?.dataset.state !== "open") return;
            prepareMotion(true);
            panelRef.current.focus({ preventScroll: true });
          });
        }}
        onEscapeKeyDown={(event) => {
          if (event.isComposing || event.keyCode === 229) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          interactedOutsideRef.current = true;
          // Interactions owned by a stacked modal decision dialog must not
          // collapse the toolbar; see isWithinModalDialog for the rationale.
          if (isWithinModalDialog(event.target)) event.preventDefault();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          if (!interactedOutsideRef.current) {
            summaryRef.current?.focus({ preventScroll: true });
            setQuickOpen(false);
          }
        }}
      >
        <div
          ref={panelRef}
          aria-hidden={!open || undefined}
          {...(!open ? { inert: "" } : {})}
          data-workbench-context-toolbar
          aria-label={`${label} 设置`}
          data-testid={`${id}-toolbar`}
          onMouseDown={(event) => event.stopPropagation()}
        >
          {children(() => changeOpen(false))}
        </div>
      </PopoverContent>
    </Popover>
  );
}
