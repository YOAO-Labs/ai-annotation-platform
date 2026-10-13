import { useEffect, useReducer, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, Search, X } from "lucide-react";
import type { ProjectRenderingConfig } from "@/api/projects";
import { HighlightText } from "@/components/ui/HighlightText";
import { Button } from "@/components/shadcn/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/shadcn/ui/dialog";
import { Empty, EmptyHeader, EmptyTitle } from "@/components/shadcn/ui/empty";
import { FieldGroup, FieldSet, FieldLegend } from "@/components/shadcn/ui/field";
import { Input } from "@/components/shadcn/ui/input";
import { Separator } from "@/components/shadcn/ui/separator";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/shadcn/ui/tabs";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import {
  NotificationPreferencesPanel,
  notificationPreferencesMatchQuery,
} from "@/components/notifications/NotificationPreferencesPanel";
import { useNotificationPreferences } from "@/hooks/useNotificationPreferences";
import {
  SettingsFieldControl,
  type SettingsControlField,
} from "../components/SettingsFieldControl";
import {
  WORKBENCH_SETTING_GROUPS,
  buildFieldPatch,
  filterWorkbenchSettings,
  getFieldValue,
  groupWorkbenchSettings,
  getVisibleWorkbenchSettingFields,
  isLocalSettingField,
  lockableFieldName,
  type WorkbenchSettingCategory,
  type WorkbenchSettingGroup,
  type WorkbenchSettingSection,
  type WorkbenchSettingValue,
} from "../state/workbenchSettingsFields";
import styles from "./WorkbenchSettingsDialog.module.css";
import { useWorkbenchConfig } from "../state/useWorkbenchConfig";
import {
  WORKBENCH_DIALOG_CONTENT_CLASS,
  workbenchDialogOverlayClass,
} from "./workbenchDialogClasses";

interface WorkbenchSettingsDialogProps {
  layoutSettings?: ReactNode;
  open: boolean;
  onClose: () => void;
  projectRenderingConfig?: ProjectRenderingConfig | null;
  hideOrphanAnnotations?: boolean;
  onToggleHideOrphans?: () => void;
  secondaryBarHidden?: boolean;
  onToggleSecondaryBar?: () => void;
  /** 关闭遮罩背景模糊(3D / 点云工作台,见 workbenchDialogClasses)。 */
  backdropBlur?: boolean;
}

interface SettingsEntry extends SettingsControlField {
  category: WorkbenchSettingCategory;
  section: WorkbenchSettingSection;
  parentKey?: string;
  value: WorkbenchSettingValue;
  locked?: boolean;
  disabled?: boolean;
  onCommit: (value: WorkbenchSettingValue) => void;
}

export function WorkbenchSettingsDialog({
  open,
  layoutSettings,
  onClose,
  projectRenderingConfig,
  hideOrphanAnnotations,
  onToggleHideOrphans,
  secondaryBarHidden,
  onToggleSecondaryBar,
  backdropBlur = true,
}: WorkbenchSettingsDialogProps) {
  // 保持 hook 挂载，关闭窗口不取消待发送的防抖保存。
  const { config, loaded, loadError, retryLoad, lockedFields, setFields } =
    useWorkbenchConfig(projectRenderingConfig);
  const [, refreshLocalFields] = useReducer((n: number) => n + 1, 0);
  const [category, setCategory] = useState<WorkbenchSettingGroup>("layout");
  const [query, setQuery] = useState("");
  const [previewField, setPreviewField] = useState<string | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const composingRef = useRef(false);
  const overlayPointerRef = useRef(false);
  const desktop = useMediaQuery("(min-width: 768px)");
  const visibleFields = getVisibleWorkbenchSettingFields();
  const categories = Object.keys(WORKBENCH_SETTING_GROUPS) as WorkbenchSettingGroup[];
  // 通知偏好与渲染配置互相独立：搜索时只读共享缓存判断是否命中。
  const preferencesQ = useNotificationPreferences();

  useEffect(() => {
    if (!open) {
      setQuery("");
      setPreviewField(null);
      setCategory("layout");
      composingRef.current = false;
    }
  }, [open]);

  const commitFocusedSetting = () => {
    const focused = document.activeElement;
    if (
      focused instanceof HTMLElement &&
      contentRef.current?.contains(focused) &&
      focused.closest('[data-testid^="setting-field-"]')
    )
      focused.blur();
  };
  const close = () => {
    commitFocusedSetting();
    onClose();
  };
  const changeCategory = (next: string) => {
    commitFocusedSetting();
    setCategory(next as WorkbenchSettingGroup);
    setQuery("");
    if (scrollRef.current) scrollRef.current.scrollTop = 0;
  };

  const entries: SettingsEntry[] = visibleFields.map((field) => {
    const lockName = lockableFieldName(field);
    const parent = visibleFields.find((candidate) => candidate.key === field.parentKey);
    return {
      ...field,
      value: getFieldValue(config, field),
      locked: lockName !== null && lockedFields.includes(lockName),
      disabled: parent ? !getFieldValue(config, parent) : false,
      onCommit: (value) => {
        if (isLocalSettingField(field)) {
          field.write(value);
          refreshLocalFields();
        } else setFields(buildFieldPatch(field, value));
      },
    };
  });
  if (onToggleHideOrphans)
    entries.push({
      key: "session.hideOrphans",
      category: "common",
      section: "behavior",
      label: "隐藏孤儿标注",
      description: "隐藏类别已从项目中删除的历史标注。仅本次工作台会话生效。",
      control: { type: "toggle" },
      value: hideOrphanAnnotations ?? false,
      onCommit: onToggleHideOrphans,
    });
  if (onToggleSecondaryBar)
    entries.push({
      key: "ui.secondary_bar_hidden",
      category: "image",
      section: "ai",
      label: "二次推理面板",
      description: "在图片任务选中标注时显示二次推理工具条，随账号同步。",
      control: { type: "toggle" },
      value: !(secondaryBarHidden ?? false),
      onCommit: onToggleSecondaryBar,
    });
  const searching = query.trim().length > 0;
  const notificationSearchMatch =
    searching && notificationPreferencesMatchQuery(preferencesQ.data?.items, query);
  const resultGroups = groupWorkbenchSettings(
    searching ? filterWorkbenchSettings(entries, query) : entries,
  ).filter((group) => searching || group.key === category);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close();
      }}
    >
      <DialogContent
        ref={contentRef}
        showCloseButton={false}
        aria-describedby={undefined}
        data-testid="workbench-settings-dialog"
        data-workbench-settings=""
        data-previewing={previewField ? "true" : undefined}
        className={`${styles.dialog} ${WORKBENCH_DIALOG_CONTENT_CLASS}`}
        overlayProps={{
          className: `${workbenchDialogOverlayClass(backdropBlur)} ${previewField ? "opacity-0" : ""}`,
          "data-testid": "workbench-settings-overlay",
          "data-workbench-settings": "",
          onPointerDown: (event) => {
            overlayPointerRef.current = event.target === event.currentTarget;
          },
          onClick: (event) => {
            if (event.target === event.currentTarget && overlayPointerRef.current) {
              event.preventDefault();
              event.stopPropagation();
              overlayPointerRef.current = false;
              close();
            }
          },
        }}
        onPointerDownCapture={() => {
          overlayPointerRef.current = false;
        }}
        onPointerDownOutside={(event) => event.preventDefault()}
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          returnFocusRef.current =
            document.activeElement instanceof HTMLElement ? document.activeElement : null;
          contentRef.current?.focus();
        }}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          const previous = returnFocusRef.current;
          const trigger =
            previous?.isConnected && previous !== document.body
              ? previous
              : document.querySelector<HTMLElement>('button[aria-label="工作台设置"]');
          trigger?.focus();
        }}
        onCompositionStart={() => {
          composingRef.current = true;
        }}
        onCompositionEnd={() => {
          composingRef.current = false;
        }}
        onEscapeKeyDown={(event) => {
          if (event.isComposing || composingRef.current) {
            event.preventDefault();
            return;
          }
          const focused = document.activeElement;
          if (
            focused instanceof HTMLSelectElement &&
            typeof CSS !== "undefined" &&
            CSS.supports("selector(:open)") &&
            focused.matches(":open")
          ) {
            // Radix 在捕获阶段处理 Esc；先关闭原生菜单，焦点留在该控件。
            event.preventDefault();
            focused.blur();
            focused.focus();
          }
        }}
      >
        <DialogTitle className="sr-only">工作台设置</DialogTitle>
        <Tabs
          value={category}
          onValueChange={changeCategory}
          orientation={desktop ? "vertical" : "horizontal"}
          className="min-h-0 flex-1 gap-0"
        >
          <aside className="flex shrink-0 flex-col gap-4 border-b border-border bg-muted/40 p-4 pt-[max(16px,env(safe-area-inset-top))] md:w-[220px] md:border-b-0 md:border-r md:pt-4">
            <Button variant="ghost" className="w-fit justify-start" onClick={close}>
              <ArrowLeft data-icon="inline-start" />
              返回工作台
            </Button>
            <div className="flex flex-col gap-2">
              <span className="text-sm font-semibold">工作台设置</span>
              <div className="relative">
                <Search
                  aria-hidden="true"
                  className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground"
                />
                <Input
                  aria-label="搜索设置"
                  placeholder="搜索设置…"
                  value={query}
                  className="h-10 pl-9 pr-8"
                  onChange={(event) => {
                    setQuery(event.target.value);
                    if (scrollRef.current) scrollRef.current.scrollTop = 0;
                  }}
                />
                {query && (
                  <button
                    type="button"
                    aria-label="清空搜索"
                    onClick={() => setQuery("")}
                    className="absolute right-0 top-0 flex h-10 w-8 items-center justify-center rounded-md text-muted-foreground hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <X className="size-3.5" />
                  </button>
                )}
              </div>
            </div>
            <TabsList
              aria-label="设置分类"
              className="h-auto w-full justify-start gap-1 overflow-x-auto bg-transparent p-0 md:items-stretch"
            >
              {categories.map((item) => (
                <TabsTrigger
                  key={item}
                  value={item}
                  onClick={() => {
                    if (searching && item === category) changeCategory(item);
                  }}
                  className="h-10 flex-none px-3 transition-none data-[state=active]:bg-card data-[state=active]:shadow-none"
                >
                  {WORKBENCH_SETTING_GROUPS[item].label}
                </TabsTrigger>
              ))}
            </TabsList>
            <footer className="mt-auto hidden flex-col gap-2 pt-6 md:flex">
              <Link
                to="/settings"
                onClick={close}
                className="text-xs text-muted-foreground hover:text-foreground hover:underline"
              >
                个人设置页 ↗
              </Link>
              <span className="text-xs text-muted-foreground">更改自动保存</span>
            </footer>
          </aside>
          <TabsContent value={category} className="m-0 flex min-h-0 min-w-0 flex-1 flex-col">
            <header className="flex shrink-0 items-start justify-between gap-4 border-b border-border px-4 py-5 md:px-8 md:py-6">
              <div className="flex min-w-0 flex-col gap-1">
                <h2 className="text-xl font-semibold">
                  {searching ? "搜索结果" : WORKBENCH_SETTING_GROUPS[category].label}
                </h2>
                <p className="text-sm text-muted-foreground">
                  {searching
                    ? "搜索全部工作台设置。"
                    : WORKBENCH_SETTING_GROUPS[category].description}
                </p>
              </div>
              <Button variant="ghost" size="icon" aria-label="关闭设置" onClick={close}>
                <X />
              </Button>
            </header>
            <div
              ref={scrollRef}
              className="min-h-0 flex-1 overflow-y-auto overscroll-contain px-4 py-2 md:px-8 md:py-4"
            >
              {loaded && !loadError && category === "layout" && !searching && layoutSettings}
              {category === "notifications" && !searching ? (
                // 通知偏好有独立的加载/重试状态；渲染配置加载失败不影响此处。
                <NotificationPreferencesPanel />
              ) : !loaded ? (
                <p role="status" className="py-8 text-sm text-muted-foreground">
                  正在加载设置…
                </p>
              ) : loadError ? (
                <Empty>
                  <EmptyHeader>
                    <EmptyTitle>无法加载设置</EmptyTitle>
                  </EmptyHeader>
                  <Button variant="outline" onClick={retryLoad}>
                    重试
                  </Button>
                </Empty>
              ) : !resultGroups.length && !notificationSearchMatch ? (
                <Empty>
                  <EmptyHeader>
                    <EmptyTitle>没有找到相关设置</EmptyTitle>
                  </EmptyHeader>
                  <Button variant="outline" onClick={() => setQuery("")}>
                    清空搜索
                  </Button>
                </Empty>
              ) : (
                <>
                  {resultGroups.flatMap((group) =>
                    group.sections.map(({ key, label, fields }, index) => (
                      <FieldSet key={key} className="gap-0 py-3">
                        <FieldLegend
                          variant="legend"
                          className="data-[variant=legend]:text-md mb-3 w-full border-b border-border pb-2 font-semibold"
                        >
                          {searching && (
                            <span className="mr-1 text-xs font-normal text-muted-foreground">
                              <HighlightText text={group.label} query={query} /> /
                            </span>
                          )}
                          <HighlightText text={label} query={searching ? query : ""} />
                        </FieldLegend>
                        <FieldGroup className="gap-0">
                          {fields.map((entry) => (
                            <SettingsFieldControl
                              key={entry.key}
                              layout="settings"
                              field={entry}
                              value={entry.value}
                              highlightQuery={searching ? query : ""}
                              locked={entry.locked}
                              disabled={entry.disabled}
                              nested={!!entry.parentKey}
                              previewing={previewField === entry.key}
                              onPreviewChange={(active) =>
                                setPreviewField(active ? entry.key : null)
                              }
                              onCommit={entry.onCommit}
                            />
                          ))}
                        </FieldGroup>
                        {index < group.sections.length - 1 && <Separator className="mt-3" />}
                      </FieldSet>
                    )),
                  )}
                  {notificationSearchMatch ? (
                    <FieldSet className="gap-0 py-3">
                      <FieldLegend
                        variant="legend"
                        className="data-[variant=legend]:text-md mb-3 w-full border-b border-border pb-2 font-semibold"
                      >
                        <span className="mr-1 text-xs font-normal text-muted-foreground">
                          通知 /
                        </span>
                        <HighlightText text="通知偏好" query={query} />
                      </FieldLegend>
                      <FieldGroup className="gap-0">
                        <NotificationPreferencesPanel filterQuery={query} />
                      </FieldGroup>
                    </FieldSet>
                  ) : null}
                </>
              )}
            </div>
            <footer className="flex shrink-0 justify-between border-t border-border px-4 py-3 pb-[max(12px,env(safe-area-inset-bottom))] text-xs text-muted-foreground md:hidden">
              <span>更改自动保存</span>
              <Link to="/settings" onClick={close}>
                个人设置页 ↗
              </Link>
            </footer>
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
  );
}
