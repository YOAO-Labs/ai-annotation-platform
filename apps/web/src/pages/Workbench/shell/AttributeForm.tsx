import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { AttributeField, AttributeSchema } from "@/api/projects";
import type { AttributesMeta, AttributeMetaEntry } from "@/types";
import { Switch } from "@/components/ui/Switch";
import { usePopover } from "@/hooks/usePopover";
import type { DirtyTracker } from "../state/useDirtyTracker";

const INPUT_CLASS =
  "appearance-none rounded-[3px] border border-border bg-card px-1.5 py-1 text-xs text-foreground";

export interface AttributeFormProps {
  schema: AttributeSchema | undefined;
  className: string;
  attributes: Record<string, unknown> | undefined;
  onChange: (next: Record<string, unknown>) => void;
  readOnly?: boolean;
  /** Keep creation drafts current before an immediate submit or cancellation. */
  immediate?: boolean;
  /**
   * v0.10.20 · I12 多选批量编辑模式: > 1 时在表单顶部渲染 banner 提示「N 个标注被选中, 修改将应用到全部」。
   * 实际 bulk-update 路径由调用方在 onChange 中分发 (走 useAnnotationBulkUpdate);
   * 表单本身只负责 UI 提示与初始值展示 (取第一个选中标注的属性).
   */
  batchCount?: number;
  /**
   * v0.10.6 M4-γ · I13.2：可选环境位。
   * - `image`（默认）：忽略 `field.mutable` 标记，行为完全 = immutable，向后兼容。
   * - `video`：mutable 字段视觉上展示「mutable」徽标，未来由 video 工作台接 keyframe override 路径。
   */
  context?: "image" | "video";
  /**
   * v0.10.6：可选 dirty tracker（useDirtyTracker 返回值）。传入时启用「批量改 → blur 一次 commit」路径：
   * - 用户连续改 attribute 字段时，dirty 累积但 onChange 立即同步本地 draft；
   * - 表单失焦（blur 出 form 容器）时调用 flush，调用方据此决定何时真正 PATCH。
   * 不传时维持原 400ms debounce 行为（v0.6.x）。
   */
  dirtyTracker?: DirtyTracker;
  /** v0.10.6：dirty tracker 模式下，需要 annotationId 才能 mark / flush。 */
  annotationId?: string;
  /** 隐藏内部「属性」标题行：外层（侧栏底部折叠头 / 悬浮框）已承载标题时使用，避免重复。 */
  hideHeading?: boolean;
  /**
   * v0.20.10 · 属性级溯源: {key: {origin, model_ref?}}。仅含 origin=ai 的键;
   * 命中的字段旁渲染一枚极轻 AI chip（hover 显 model）。人工改该属性并保存后,
   * 后端会删对应 meta,刷新后 chip 自然消失。
   */
  attributesMeta?: AttributesMeta | null;
  /**
   * Increment B · 属性快捷键区域（显式 opt-in，仅图片选中卡两类消费者启用）。
   * 开启后表单外围渲染一个可聚焦的非编辑区域：点击 / 聚焦其标题（或区域本身）后，
   * 数字键属性快捷键才生效；焦点离开区域立即恢复画布类别键所有权。
   * 原生 input/select/contenteditable 控件始终保留自己的键盘行为。
   */
  shortcutRegion?: boolean;
}

/** v0.20.10 · AI 溯源 chip 的 hover 文案: 尽量显 model / backend。 */
function aiChipTitle(entry: AttributeMetaEntry | undefined): string {
  const ref = entry?.model_ref;
  const parts: string[] = [];
  if (ref?.model_id) parts.push(`模型 ${ref.model_id}`);
  if (ref?.backend_id) parts.push(`后端 ${ref.backend_id}`);
  return parts.length > 0 ? `AI 填充 · ${parts.join(" · ")}` : "AI 填充";
}

/** 判断 field 在当前 class + 当前值组合下是否应展示。 */
function isVisible(
  field: AttributeField,
  className: string,
  values: Record<string, unknown>,
): boolean {
  const applies = field.applies_to ?? "*";
  if (applies !== "*") {
    if (!Array.isArray(applies) || !applies.includes(className)) return false;
  }
  if (field.visible_if) {
    const cur = values[field.visible_if.key];
    if (cur !== field.visible_if.equals) return false;
  }
  return true;
}

/** 列出当前 class 下所有 required 且尚未填的 field key（缺失项）。 */
export function getMissingRequired(
  schema: AttributeSchema | undefined,
  className: string,
  attributes: Record<string, unknown> | undefined,
): string[] {
  if (!schema) return [];
  const values = attributes ?? {};
  const missing: string[] = [];
  for (const f of schema.fields ?? []) {
    if (!f.required) continue;
    if (!isVisible(f, className, values)) continue;
    const v = values[f.key];
    const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
    if (empty) missing.push(f.key);
  }
  return missing;
}

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

export function AttributeForm({
  schema,
  className,
  attributes,
  onChange,
  readOnly,
  immediate = false,
  context = "image",
  dirtyTracker,
  annotationId,
  batchCount,
  hideHeading,
  attributesMeta,
  shortcutRegion = false,
}: AttributeFormProps) {
  const [draft, setDraft] = useState<Record<string, unknown>>(attributes ?? {});
  // Increment B · 属性快捷键区域的激活态（焦点在区域内 = 激活）。
  const [regionActive, setRegionActive] = useState(false);
  const regionRef = useRef<HTMLDivElement>(null);
  const lastFromUpstream = useRef<Record<string, unknown>>(attributes ?? {});
  // v0.10.6：保留最新 draft 引用，blur flush 时取最新值上抛
  const draftRef = useRef(draft);
  const immediateRef = useRef(immediate);
  immediateRef.current = immediate;
  useEffect(() => {
    draftRef.current = draft;
  }, [draft]);
  // v0.16.8：保留最新 onChange，卸载补 flush（见底部 cleanup）时用最新回调，避免闭包捕获首渲染的旧 onChange
  const onChangeRef = useRef(onChange);
  useEffect(() => {
    onChangeRef.current = onChange;
  }, [onChange]);

  // 上游 attributes 变化（切选中标注 / 切类别）时同步本地 draft，避免输入残留。
  useEffect(() => {
    const next = attributes ?? {};
    if (JSON.stringify(next) !== JSON.stringify(lastFromUpstream.current)) {
      lastFromUpstream.current = next;
      draftRef.current = next;
      setDraft(next);
    }
  }, [attributes]);

  // 防抖 400ms 上抛（dirty tracker 模式下旁路：dirty 累积，blur 时一次 flush）
  const debounceRef = useRef<number | null>(null);
  const useDirty = !immediate && !!(dirtyTracker && annotationId);

  useEffect(() => {
    if (immediate && debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
    }
  }, [immediate]);

  const scheduleCommit = (next: Record<string, unknown>) => {
    draftRef.current = next;
    setDraft(next);
    if (immediate) {
      if (debounceRef.current !== null) window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
      onChange(next);
      return;
    }
    if (useDirty) {
      // dirty tracker 模式：标脏即可，不立即触发 onChange；等 blur flush
      dirtyTracker!.markDirty(annotationId!, "attributes");
      return;
    }
    if (debounceRef.current) window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      onChange(next);
      debounceRef.current = null;
    }, 400) as unknown as number;
  };

  // v0.10.6：form 整体 blur 时（焦点离开 form 容器）flush
  const handleFormBlur = (e: React.FocusEvent<HTMLDivElement>) => {
    // relatedTarget 仍在 form 内 → 字段间跳转，不算 blur 出
    if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
    if (useDirty) {
      dirtyTracker!.flush(annotationId!, () => {
        onChange(draftRef.current);
      });
      return;
    }
    // 提交/切题通常先让表单失焦；在这里立即释放普通 400ms 防抖，
    // 让上层现有 mutation pending 门禁可以确认最后一笔属性修改。
    if (debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
      debounceRef.current = null;
      if (!immediateRef.current) onChangeRef.current(draftRef.current);
    }
  };

  // v0.16.8 修复「改属性看似改了实则没保存」：debounce 路径下，组件卸载前若仍有未到点的提交
  // （如 ClassPickerPopover 内联属性编辑 <400ms 内关闭弹层），用最新 draft 补 flush 一次，
  // 避免待提交的 onChange 被 clearTimeout 丢弃。debounceRef 为 null（已 flush / dirty 模式）时不触发，杜绝重复提交。
  useEffect(
    () => () => {
      if (debounceRef.current) {
        window.clearTimeout(debounceRef.current);
        debounceRef.current = null;
        if (!immediateRef.current) onChangeRef.current(draftRef.current);
      }
    },
    [],
  );

  const visible = useMemo(
    () => (schema?.fields ?? []).filter((f) => isVisible(f, className, draft)),
    [schema, className, draft],
  );

  if (!schema || visible.length === 0) return null;

  const missing = getMissingRequired(schema, className, draft);

  const hotkeyBadgeTitle = (hotkey: string) =>
    shortcutRegion
      ? `聚焦「属性快捷键」区域后按 ${hotkey} 切换该属性`
      : `属性快捷键 ${hotkey}：在图片选中卡聚焦「属性快捷键」区域后生效`;

  const fieldsList = visible.map((f) => {
    const v = draft[f.key];
    const isMissing = f.required && missing.includes(f.key);
    const setValue = (newV: unknown) => scheduleCommit({ ...draft, [f.key]: newV });
    return (
      <label
        key={f.key}
        className={cn(
          "flex flex-col gap-1 rounded border border-transparent px-1.5 py-1",
          f.type === "boolean" && "flex-row items-center justify-between gap-2",
          isMissing && "border-rose-400/60 bg-status-danger-soft",
        )}
      >
        <span className="inline-flex items-center gap-1.5 text-xs text-foreground">
          {f.label}
          {f.required && <span className="ml-1 text-status-danger">*</span>}
          {/* v0.20.10 · AI 属性溯源 chip: origin=ai 的字段旁一枚极轻标记, hover 显 model。 */}
          {attributesMeta?.[f.key]?.origin === "ai" && (
            <span
              title={aiChipTitle(attributesMeta[f.key])}
              data-testid={`attr-ai-origin-${f.key}`}
              className="rounded-[3px] border border-brand/30 bg-brand/10 px-1 py-px text-2xs font-semibold leading-[1.2] text-brand"
            >
              ✦ AI
            </span>
          )}
          {/* v0.10.6 M4-γ · I13.2：视频任务下 mutable 字段标记徽标，提示「逐 keyframe 可变」语义。 */}
          {context === "video" && f.mutable === true && (
            <span
              title="逐 keyframe 可变（mutable）"
              data-testid={`attr-mutable-badge-${f.key}`}
              className="rounded-[3px] border border-amber-500/40 bg-status-caution-soft px-1.5 py-px text-2xs font-semibold uppercase leading-[1.2] tracking-[0.3px] text-status-caution"
            >
              逐帧
            </span>
          )}
          {f.description && <DescriptionPopover description={f.description} />}
          {f.hotkey && (f.type === "boolean" || f.type === "select") && (
            <span
              className="mono rounded-[3px] border border-b-2 border-brand/30 bg-brand/10 px-1.5 py-px text-2xs font-semibold text-brand"
              title={hotkeyBadgeTitle(f.hotkey)}
            >
              ⌨ {f.hotkey}
            </span>
          )}
        </span>
        {f.type === "text" && (
          <input
            type="text"
            value={(v as string) ?? ""}
            disabled={readOnly}
            onChange={(e) => setValue(e.target.value)}
            data-attribute-key={f.key}
            className={INPUT_CLASS}
          />
        )}
        {f.type === "number" && (
          <input
            type="number"
            value={(v as number | string | undefined) ?? ""}
            min={f.min ?? undefined}
            max={f.max ?? undefined}
            disabled={readOnly}
            onChange={(e) => {
              const n = e.target.value === "" ? undefined : Number(e.target.value);
              setValue(n);
            }}
            data-attribute-key={f.key}
            className={INPUT_CLASS}
          />
        )}
        {f.type === "boolean" && (
          <Switch
            checked={!!v}
            disabled={readOnly}
            onChange={(next) => setValue(next)}
            data-attribute-key={f.key}
          />
        )}
        {f.type === "select" && (
          <select
            value={(v as string) ?? ""}
            disabled={readOnly}
            onChange={(e) => setValue(e.target.value || undefined)}
            data-attribute-key={f.key}
            className={`${INPUT_CLASS} cursor-pointer`}
          >
            <option value="">—</option>
            {f.options?.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        )}
        {f.type === "multiselect" && (
          <select
            multiple
            value={Array.isArray(v) ? (v as string[]) : []}
            disabled={readOnly}
            onChange={(e) => {
              const arr = Array.from(e.target.selectedOptions).map((o) => o.value);
              setValue(arr);
            }}
            data-attribute-key={f.key}
            className={`${INPUT_CLASS} h-20`}
          >
            {f.options?.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        )}
        {f.type === "range" && (
          <div className="flex items-center gap-2">
            <input
              type="range"
              min={f.min ?? 0}
              max={f.max ?? 100}
              value={typeof v === "number" ? v : (f.min ?? 0)}
              disabled={readOnly}
              onChange={(e) => setValue(Number(e.target.value))}
              data-attribute-key={f.key}
              className="flex-1 accent-brand"
            />
            <span className="mono min-w-[2.5ch] text-right text-xs text-muted-foreground">
              {typeof v === "number" ? v : (f.min ?? 0)}
            </span>
          </div>
        )}
      </label>
    );
  });

  return (
    <div
      className="flex flex-col gap-1.5 border-t border-border px-3 pb-2.5 pt-2"
      onBlur={handleFormBlur}
    >
      {batchCount && batchCount > 1 && (
        <div
          className="rounded border border-amber-500 bg-status-caution-soft px-2.5 py-1.5 text-xs text-status-caution"
          data-testid="attribute-form-batch-banner"
          role="status"
        >
          <b>{batchCount}</b> 个标注被选中, 修改将应用到全部
        </div>
      )}
      {!hideHeading && (
        <div className="text-xs font-semibold uppercase tracking-[0.4px] text-muted-foreground">
          属性{" "}
          {missing.length > 0 && (
            <span className="text-status-danger">· {missing.length} 项必填未填</span>
          )}
        </div>
      )}
      {shortcutRegion ? (
        <div className="flex flex-col gap-1">
          <button
            type="button"
            data-testid="attribute-shortcut-region-heading"
            onClick={() => regionRef.current?.focus()}
            className="w-fit appearance-none rounded-sm bg-transparent p-0 text-2xs font-semibold uppercase tracking-[0.4px] text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title="点击启用属性快捷键；焦点离开区域自动停用"
          >
            属性快捷键
          </button>
          <div
            ref={regionRef}
            tabIndex={0}
            data-attribute-shortcut-region=""
            data-region-active={regionActive || undefined}
            data-testid="attribute-shortcut-region"
            role="group"
            aria-label="属性快捷键区域"
            onFocus={(e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
              setRegionActive(true);
            }}
            onBlur={(e) => {
              if (e.currentTarget.contains(e.relatedTarget as Node | null)) return;
              setRegionActive(false);
            }}
            className={cn(
              "flex flex-col gap-0.5 rounded-md border px-1 py-0.5 outline-none transition-none",
              regionActive ? "border-brand/50 ring-2 ring-brand/15" : "border-dashed border-border",
            )}
          >
            {fieldsList}
            <span
              className="px-1 pb-0.5 text-2xs"
              role="status"
              data-testid="attribute-shortcut-region-hint"
            >
              {regionActive ? (
                <span className="text-brand">属性快捷键已启用 · 数字键切换属性，Esc 退出</span>
              ) : (
                <span className="text-muted-foreground">
                  点击此区域启用后，数字键切换属性；画布数字键仍切换类别
                </span>
              )}
            </span>
          </div>
        </div>
      ) : (
        fieldsList
      )}
    </div>
  );
}

/** v0.6.4：description 支持 markdown（链接 / 加粗 / 列表 / 换行）。
 *  hover 或 focus 时弹出 popover，点击外部关闭。链接强制 target=_blank。
 *  GFM 启用，但禁 raw HTML（react-markdown 默认不开 rehype-raw，避免 XSS）。
 */
function DescriptionPopover({ description }: { description: string }) {
  // v0.7.0：迁移到 usePopover（统一 click-outside + ESC 行为）
  const pop = usePopover();
  const containerRef = useRef<HTMLSpanElement | null>(null);

  return (
    <span
      ref={(node) => {
        containerRef.current = node;
        pop.anchorRef.current = node;
      }}
      onMouseEnter={() => pop.setOpen(true)}
      onMouseLeave={() => pop.close()}
      className="relative inline-flex"
    >
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          pop.toggle();
        }}
        aria-label={`查看说明：${description.slice(0, 50)}`}
        className="inline-flex size-3.5 cursor-help appearance-none items-center justify-center rounded-full border border-border bg-muted p-0 text-micro font-semibold text-muted-foreground"
      >
        i
      </button>
      {pop.open && (
        <div
          ref={pop.popoverRef as React.MutableRefObject<HTMLDivElement | null>}
          role="tooltip"
          className="absolute left-0 top-[calc(100%+4px)] z-workbench-modal min-w-[180px] max-w-[280px] rounded border border-border bg-card px-2.5 py-2 text-xs leading-normal text-foreground shadow-lg [pointer-events:auto]"
        >
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              a: ({ href, children }) => (
                <a href={href} target="_blank" rel="noopener noreferrer" className="text-brand">
                  {children}
                </a>
              ),
              p: ({ children }) => <p className="mb-1.5">{children}</p>,
              ul: ({ children }) => <ul className="mb-1.5 pl-4">{children}</ul>,
              ol: ({ children }) => <ol className="mb-1.5 pl-4">{children}</ol>,
              code: ({ children }) => (
                <code className="rounded-[3px] border border-border bg-muted px-1 py-px font-mono text-xs text-muted-foreground">
                  {children}
                </code>
              ),
              strong: ({ children }) => (
                <strong className="font-semibold text-foreground">{children}</strong>
              ),
              em: ({ children }) => <em className="text-muted-foreground">{children}</em>,
              li: ({ children }) => <li className="mb-0.5">{children}</li>,
            }}
          >
            {description}
          </ReactMarkdown>
        </div>
      )}
    </span>
  );
}
