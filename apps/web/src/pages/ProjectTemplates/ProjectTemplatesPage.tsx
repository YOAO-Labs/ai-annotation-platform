import { FilterGroup, FilterToggle } from "@/components/filters/FilterControls";
// v0.10.14 · E2 · 模板库列表页. 三个 tab (我的 / 组织 / 公共), 搜索 + type
// filter, 卡片列表. 操作: 应用 (跳 Wizard with template_id) / 克隆 / 编辑 / 删除.
// 新建入口: + 新建模板 (空白) / 从已有项目导出.

import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { SearchInput } from "@/components/ui/SearchInput";
import { confirmDialog } from "@/components/ui/decisionDialog";
import { useToastStore } from "@/components/ui/Toast";
import { useAuthStore } from "@/stores/authStore";
import {
  useDeleteProjectTemplate,
  useDuplicateProjectTemplate,
  useProjectTemplates,
} from "@/hooks/useProjectTemplates";
import type { ProjectTemplateOut, TemplateScope } from "@/api/projectTemplates";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";

import { CreateFromProjectDialog } from "./CreateFromProjectDialog";
import { TemplateCard } from "./TemplateCard";
import { TemplateEditModal } from "./TemplateEditModal";
import styles from "./ProjectTemplatesPage.module.css";
import {
  EMPTY_PROJECT_TEMPLATES_URL_STATE,
  PROJECT_TEMPLATE_FILTER_KEYS,
  projectTemplatesUrlCodec,
  templateScopeForApi,
} from "./projectTemplatesUrlState";

const CreateProjectWizard = lazy(() =>
  import("@/components/projects/CreateProjectWizard").then((m) => ({
    default: m.CreateProjectWizard,
  })),
);

const TABS = ["我的模板", "组织模板", "公共模板", "全部"] as const;
type TabLabel = (typeof TABS)[number];

const SCOPE_BY_TAB: Record<TabLabel, TemplateScope | undefined> = {
  我的模板: "private",
  组织模板: "organization",
  公共模板: "public",
  全部: undefined,
};

export function ProjectTemplatesPage() {
  const pushToast = useToastStore((s) => s.push);
  const me = useAuthStore((s) => s.user);
  const [editTarget, setEditTarget] = useState<ProjectTemplateOut | undefined>();
  const [editOpen, setEditOpen] = useState(false);
  const [fromProjectOpen, setFromProjectOpen] = useState(false);
  const [applyTemplateId, setApplyTemplateId] = useState<string | null>(null);
  const urlState = useUrlFilterState({
    codec: projectTemplatesUrlCodec,
    defaults: EMPTY_PROJECT_TEMPLATES_URL_STATE,
    ownedKeys: PROJECT_TEMPLATE_FILTER_KEYS,
  });
  const currentUrl = urlState.state;
  const [search, setSearch] = useState(currentUrl.query);
  const [searchFlushKey, setSearchFlushKey] = useState(0);
  const localSearchWriteRef = useRef<string | null>(null);
  useEffect(() => {
    if (localSearchWriteRef.current === currentUrl.query) {
      localSearchWriteRef.current = null;
      return;
    }
    setSearch(currentUrl.query);
    setSearchFlushKey((value) => value + 1);
  }, [currentUrl.query]);
  const debouncedSearch = useDebouncedValue(search, 250, searchFlushKey);
  const searchForRequest =
    localSearchWriteRef.current === null && search.trim() !== currentUrl.query
      ? currentUrl.query
      : debouncedSearch;

  const activeTab =
    currentUrl.scope === "organization"
      ? "组织模板"
      : currentUrl.scope === "public"
        ? "公共模板"
        : currentUrl.scope === "all"
          ? "全部"
          : "我的模板";

  const scopeFilter = templateScopeForApi(currentUrl.scope);
  const list = useProjectTemplates(
    scopeFilter
      ? { scope: scopeFilter, search: searchForRequest.trim() || undefined }
      : { search: searchForRequest.trim() || undefined },
  );
  const remove = useDeleteProjectTemplate();
  const duplicate = useDuplicateProjectTemplate();

  const templates = useMemo(() => list.data ?? [], [list.data]);

  const handleApply = (t: ProjectTemplateOut) => {
    setApplyTemplateId(t.id);
  };

  const handleDuplicate = (t: ProjectTemplateOut) => {
    duplicate.mutate(t.id, {
      onSuccess: () => pushToast({ msg: "已克隆模板", kind: "success" }),
      onError: (err) =>
        pushToast({
          msg: err instanceof Error ? err.message : "克隆失败",
          kind: "warning",
        }),
    });
  };

  const handleEdit = (t: ProjectTemplateOut) => {
    setEditTarget(t);
    setEditOpen(true);
  };

  const handleDelete = async (t: ProjectTemplateOut) => {
    if (
      !(await confirmDialog({
        tone: "danger",
        title: "删除模板",
        description: `将删除模板「${t.name}」, 删除不会影响历史已创建的项目.`,
        details: `已使用 ${t.usage_count} 次`,
        confirmLabel: "删除",
      }))
    ) {
      return;
    }
    remove.mutate(t.id, {
      onSuccess: () => pushToast({ msg: "已删除", kind: "success" }),
      onError: (err) =>
        pushToast({
          msg: err instanceof Error ? err.message : "删除失败",
          kind: "warning",
        }),
    });
  };

  const openNew = () => {
    setEditTarget(undefined);
    setEditOpen(true);
  };

  const updateSearch = (next: string) => {
    setSearch(next);
    localSearchWriteRef.current = next.trim();
    urlState.patch({ query: next });
  };
  const updateTab = (next: string) => {
    const tab = next as TabLabel;
    const scope = SCOPE_BY_TAB[tab];
    urlState.patch({ scope: scope ?? "all" }, { replace: false });
  };

  const isSuperAdmin = me?.role === "super_admin";

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <div>
          <h2 className={styles.title}>项目模板库</h2>
          <p className={styles.titleHint}>
            v0.10.14 · 模板是独立资产, 应用模板时把字段复制进新项目; 后续修改模板不影响已有项目.
            与"从已有项目复制"功能并存.
          </p>
        </div>
        <div className={styles.toolbar}>
          <Button onClick={openNew} data-testid="template-new-btn">
            <Icon name="plus" size={12} /> 新建模板
          </Button>
          <Button variant="ghost" onClick={() => setFromProjectOpen(true)}>
            <Icon name="copy" size={12} /> 从已有项目导出
          </Button>
        </div>
      </div>

      <div className={styles.toolbar}>
        <FilterGroup label="范围" hideLabel aria-label="模板范围">
          {TABS.map((option) => (
            <FilterToggle
              key={option}
              active={activeTab === option}
              onClick={() => updateTab(option)}
            >
              {option}
            </FilterToggle>
          ))}
        </FilterGroup>
        <div className={styles.grow} />
        <SearchInput
          value={search}
          onChange={updateSearch}
          placeholder="搜索模板名称…"
          width={220}
        />
      </div>

      {list.isLoading ? (
        <div className={styles.empty}>加载中…</div>
      ) : templates.length === 0 ? (
        <div className={styles.empty}>
          暂无模板
          {activeTab === "我的模板" ? "，点击「新建模板」开始" : ""}
        </div>
      ) : (
        <div className={styles.grid}>
          {templates.map((t) => (
            <TemplateCard
              key={t.id}
              template={t}
              canEdit={isSuperAdmin || t.created_by === me?.id}
              onApply={() => handleApply(t)}
              onDuplicate={() => handleDuplicate(t)}
              onEdit={() => handleEdit(t)}
              onDelete={() => handleDelete(t)}
            />
          ))}
        </div>
      )}

      <TemplateEditModal open={editOpen} onClose={() => setEditOpen(false)} initial={editTarget} />

      <CreateFromProjectDialog open={fromProjectOpen} onClose={() => setFromProjectOpen(false)} />

      <Suspense fallback={null}>
        {applyTemplateId ? (
          <CreateProjectWizard
            open
            onClose={() => setApplyTemplateId(null)}
            templateId={applyTemplateId}
          />
        ) : null}
      </Suspense>
    </div>
  );
}
