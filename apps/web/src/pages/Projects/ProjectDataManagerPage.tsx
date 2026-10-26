import { ListPagination } from "@/components/ui/ListPagination";
import { listStyles } from "@/components/ui/listStyles";
import { useEffect, useMemo, useRef, useState, type RefObject } from "react";
import { Navigate, useParams, useSearchParams } from "react-router-dom";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { Thumbnail } from "@/components/Thumbnail";
import { useProject } from "@/hooks/useProjects";
import { useProjectAccess } from "@/hooks/useProjectAccess";
import { useTask } from "@/hooks/useTasks";
import type { ProjectResponse } from "@/api/projects";
import {
  useCreateTaskView,
  useDataManagerSchema,
  useDataManagerSummary,
  useDeleteTaskView,
  useProjectTaskQuery,
  useTaskViews,
  useUpdateTaskView,
} from "@/hooks/useTaskViews";
import type {
  DataManagerFilterField,
  DataManagerEntityScope,
  DataManagerTask,
  TaskFilterOp,
  TaskFilterRule,
  TaskSortItem,
} from "@/api/taskViews";
import { useAuthStore } from "@/stores/authStore";
import { usePermissions } from "@/hooks/usePermissions";
import { cn } from "@/lib/utils";
import { Input } from "@/components/shadcn/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/shadcn/ui/popover";
import { Skeleton } from "@/components/shadcn/ui/skeleton";
import { DataManagerAnalyticsPanel } from "./data-manager/DataManagerAnalyticsPanel";
import {
  DataManagerFilterBar,
  type DataManagerFilterChip,
  type DataManagerQuickFilter,
} from "./data-manager/DataManagerFilterBar";
import { FilterValueEditor } from "@/components/filters/FilterValueEditor";
import { useFilterDraftValidity } from "@/components/filters/useFilterDraftValidity";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { usePageTableHeader } from "./data-manager/usePageTableHeader";
import { useUrlFilterState } from "@/hooks/useUrlFilterState";
import { formatFilterDraft, parseFilterValue } from "@/lib/filters/filterValues";
import { filterOperatorLabel } from "@/lib/filters/types";
import {
  combineKeyword,
  collapseEmptyGroups,
  hasNestedGroups,
  isExpressionValid,
  isEmptyFilter,
  isFilterGroup,
  isFilterRule,
  validateFilterStructure,
  appendRule,
  removeAtPath,
  splitKeyword,
  updateRuleAtPath,
  type DataManagerFilterExpression,
} from "./data-manager/dataManagerFilterExpression";
import { DataManagerExpressionEditor } from "./data-manager/DataManagerExpressionEditor";
import {
  DataManagerProjectOverview,
  DataManagerSummaryStrip,
} from "./data-manager/DataManagerOverview";
import { DataManagerFrame } from "./data-manager/DataManagerFrame";
import { DataManagerTaskActions } from "./data-manager/DataManagerTaskActions";
import {
  ProjectMembersPerformance,
  ProjectPerformanceSummary,
} from "./data-manager/ProjectMembersPerformance";
import { DataManagerLensTabs } from "./data-manager/DataManagerLensTabs";
import { DataManagerViewActions } from "./data-manager/DataManagerViewActions";
import { EntityDataManagerLens } from "./data-manager/EntityDataManagerLens";
import { TaskMatchesSheet } from "./data-manager/TaskMatchesSheet";
import {
  DATA_MANAGER_FILTER_KEYS,
  dataManagerUrlCodec,
  dataManagerViewKey,
  findDataManagerView,
  normalizeDataManagerColumns,
  parseDataManagerUrl,
  requestedDataManagerViewKey,
  resolveDataManagerSort,
  resolveDataManagerViewKey,
  shouldUseDataManagerUrlOverrides,
  type DataManagerLayout,
  type DataManagerSection,
  updateDataManagerUrl,
} from "./data-manager/dataManagerUrlState";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/shadcn/ui/dialog";
import { useToastStore } from "@/components/ui/Toast";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/shadcn/ui/select";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/shadcn/ui/alert-dialog";

const MAX_SELECTED_TASKS = 200;

const EMPTY_DATA_MANAGER_URL_STATE = {
  section: "data" as const,
  lens: "tasks" as const,
  view: null,
  query: "",
  filter: null,
  sort: null,
  columns: null,
  selected: null,
  selectedTasks: null,
  layout: "list" as const,
};

// UA-safe 表单基线(无全局 preflight 期间,原生 select/input 需消浏览器默认样式)
const FIELD_CLASS =
  "h-8 w-full appearance-none rounded-sm border border-border bg-background px-2 py-1.5 text-foreground disabled:bg-muted disabled:text-muted-foreground";

const FALLBACK_FILTER_FIELDS: DataManagerFilterField[] = [
  {
    key: "task.status",
    label: "任务状态",
    group: "工作流",
    value_type: "select",
    operators: ["eq", "ne", "in"],
    options: [],
    expensive: false,
    tool_unit_id: null,
    attribute_key: null,
  },
  {
    key: "annotation.annotation_count",
    label: "标注数",
    group: "标注",
    value_type: "number",
    operators: ["eq", "ne", "gt", "gte", "lt", "lte", "in"],
    options: [],
    expensive: false,
    tool_unit_id: null,
    attribute_key: null,
  },
  {
    key: "annotation.class_name",
    label: "标注类别",
    group: "标注",
    value_type: "select",
    operators: ["exists", "eq", "in"],
    options: [],
    expensive: false,
    tool_unit_id: null,
    attribute_key: null,
  },
];

const COLUMN_OPTIONS = [
  { key: "display_id", label: "任务" },
  { key: "file_name", label: "文件名" },
  { key: "status", label: "状态" },
  { key: "annotation_count", label: "标注" },
  { key: "pending_prediction_shape_count", label: "AI 检测待审" },
  { key: "low_confidence_prediction_shape_count", label: "低置信 AI 待审 (<50%)" },
  { key: "pending_tracker_job_count", label: "AI 追踪待审" },
  { key: "unresolved_issue_count", label: "未解决问题" },
  { key: "comment_count", label: "评论" },
  { key: "annotation_source_counts", label: "来源" },
  { key: "track_count", label: "轨迹" },
  { key: "last_activity_at", label: "最近活动" },
  { key: "assignee", label: "标注员" },
  { key: "reviewer", label: "审核员" },
] as const;

const DEFAULT_COLUMNS = COLUMN_OPTIONS.slice(0, 12).map((item) => item.key);
type SelectedTask = Pick<DataManagerTask, "id" | "project_id" | "display_id" | "file_name">;

interface RuleChipDraft {
  field: string;
  op: TaskFilterOp;
  value: string;
}

function formatDate(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function statusLabel(status: string) {
  switch (status) {
    case "pending":
      return "待标注";
    case "in_progress":
      return "标注中";
    case "review":
      return "待审核";
    case "completed":
      return "已完成";
    case "uploading":
      return "上传中";
    default:
      return status;
  }
}

function ruleValueLabel(rule: RuleChipDraft, fields: DataManagerFilterField[]) {
  if (rule.op === "exists" || rule.op === "missing") return filterOperatorLabel(rule.op);
  if (rule.value === "null") return `${filterOperatorLabel(rule.op)} 空值`;
  const field = fields.find((item) => item.key === rule.field);
  const option = field?.options.find((item) => item.value === rule.value);
  const value = option?.label ?? rule.value.trim();
  return `${filterOperatorLabel(rule.op)} ${value || "未填写"}`;
}

function normalizeTaskSelection(values: string[] | null | undefined) {
  return [...new Set((values ?? []).filter(Boolean))].slice(0, MAX_SELECTED_TASKS);
}

export function ProjectDataManagerPage() {
  const { id = "" } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const { data: project, isLoading, error } = useProject(id);
  const dataScrollRef = useRef<HTMLDivElement>(null);
  const schemaQ = useDataManagerSchema(id, "tasks");
  const projectAccess = useProjectAccess(id);
  const user = useAuthStore((state) => state.user);
  const [dataDirty, setDataDirty] = useState(false);
  const [pendingSection, setPendingSection] = useState<DataManagerSection | null>(null);
  const urlState = parseDataManagerUrl(searchParams);
  const requestedScope = urlState.lens;
  const availableScopes = schemaQ.data?.available_entity_scopes ?? ["tasks"];
  const scope = availableScopes.includes(requestedScope) ? requestedScope : "tasks";
  const canViewMembers = projectAccess.hasCapability("performance.read");
  const requestedSection: DataManagerSection = urlState.section ?? "data";
  const section: DataManagerSection =
    requestedSection === "members" && !canViewMembers ? "data" : requestedSection;

  useEffect(() => {
    if (!schemaQ.data || requestedScope === scope) return;
    const next = updateDataManagerUrl(searchParams, {
      lens: scope,
      view: null,
      query: "",
      filter: null,
      sort: null,
      columns: null,
      selected: null,
    });
    setSearchParams(next, { replace: true });
  }, [requestedScope, schemaQ.data, scope, searchParams, setSearchParams]);

  useEffect(() => {
    if (!project || requestedSection !== "members" || canViewMembers) return;
    setSearchParams(
      updateDataManagerUrl(searchParams, {
        ...urlState,
        section: "data",
        selected: null,
        selectedTasks: null,
      }),
      { replace: true },
    );
  }, [canViewMembers, project, requestedSection, searchParams, setSearchParams, urlState]);

  useEffect(() => {
    if (section !== "data" && dataDirty) setDataDirty(false);
  }, [dataDirty, section]);

  useEffect(() => {
    setPendingSection(null);
    setDataDirty(false);
  }, [id, user?.id]);

  if (isLoading || schemaQ.isLoading) {
    return <div className="p-15 text-center text-muted-foreground">加载中...</div>;
  }
  if (error || !project) return <Navigate to="/unauthorized" replace />;

  const changeSection = (nextSection: DataManagerSection) => {
    if (nextSection === "members" && !canViewMembers) return;
    if (section === "data" && nextSection !== "data" && dataDirty) {
      setPendingSection(nextSection);
      return;
    }
    const next = updateDataManagerUrl(searchParams, {
      section: nextSection,
      lens: scope,
      view: urlState.view,
      query: urlState.query,
      filter: urlState.filter,
      sort: urlState.sort,
      columns: urlState.columns,
      selected: urlState.selected,
      layout: urlState.layout,
      selectedTasks: nextSection === "data" ? urlState.selectedTasks : null,
    });
    setSearchParams(next);
  };

  const changeScope = (nextScope: DataManagerEntityScope) => {
    const next = updateDataManagerUrl(searchParams, {
      section: "data",
      lens: nextScope,
      view: null,
      query: "",
      filter: null,
      sort: null,
      columns: null,
      selected: null,
      selectedTasks: null,
    });
    setSearchParams(next);
  };

  const content =
    section === "members" ? (
      <div className="min-h-0 flex-1 overflow-y-auto">
        <ProjectMembersPerformance key={`${id}:${user?.id}`} projectId={id} />
      </div>
    ) : section === "overview" ? (
      <DataManagerProjectOverview
        projectId={id}
        summaryFilter={{}}
        onDrill={(rule) => {
          const next = updateDataManagerUrl(searchParams, {
            section: "data",
            lens: "tasks",
            view: "builtin:all",
            query: "",
            filter: rule,
            sort: null,
            columns: null,
            selected: null,
            selectedTasks: null,
            layout: "list",
          });
          setSearchParams(next);
        }}
      >
        {canViewMembers && (
          <ProjectPerformanceSummary
            key={`${id}:${user?.id}`}
            projectId={id}
            onOpenMembers={() => changeSection("members")}
          />
        )}
      </DataManagerProjectOverview>
    ) : scope === "objects" || scope === "tracks" ? (
      <EntityDataManagerLens
        key={`${id}:${user?.id ?? "anonymous"}:${scope}`}
        projectId={id}
        projectName={project.name}
        projectDisplayId={project.display_id}
        projectOwnerId={project.owner_id}
        scope={scope}
        availableScopes={availableScopes}
        onScopeChange={changeScope}
        onDirtyChange={setDataDirty}
        scrollContainerRef={dataScrollRef}
      />
    ) : (
      <TaskDataManagerPage
        project={project}
        availableScopes={availableScopes}
        onScopeChange={changeScope}
        onDirtyChange={setDataDirty}
        scrollContainerRef={dataScrollRef}
      />
    );

  return (
    <DataManagerFrame
      projectId={id}
      projectName={project.name}
      projectDisplayId={project.display_id}
      section={section}
      onSectionChange={changeSection}
      canViewMembers={canViewMembers}
      dataScrollRef={dataScrollRef}
    >
      {content}
      <AlertDialog
        open={Boolean(pendingSection)}
        onOpenChange={(open) => !open && setPendingSection(null)}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>离开数据浏览？</AlertDialogTitle>
            <AlertDialogDescription>
              当前修改尚未保存到视图。已应用的条件会保留在链接中，未完成的输入和任务选择将被清除。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>继续编辑</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (!pendingSection) return;
                const next = updateDataManagerUrl(searchParams, {
                  ...urlState,
                  section: pendingSection,
                  selected: null,
                  selectedTasks: null,
                });
                setPendingSection(null);
                setDataDirty(false);
                setSearchParams(next);
              }}
            >
              继续切换
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </DataManagerFrame>
  );
}

function TaskDataManagerPage({
  project,
  availableScopes,
  onScopeChange,
  onDirtyChange,
  scrollContainerRef,
}: {
  project: ProjectResponse;
  availableScopes: DataManagerEntityScope[];
  onScopeChange: (scope: DataManagerEntityScope) => void;
  onDirtyChange?: (dirty: boolean) => void;
  scrollContainerRef: RefObject<HTMLDivElement | null>;
}) {
  const { id = "" } = useParams<{ id: string }>();
  const [searchParams, setSearchParams] = useSearchParams();
  const urlState = useUrlFilterState({
    codec: dataManagerUrlCodec,
    defaults: EMPTY_DATA_MANAGER_URL_STATE,
    ownedKeys: DATA_MANAGER_FILTER_KEYS,
  });
  const currentUrl = urlState.state;
  const { role } = usePermissions();
  const user = useAuthStore((s) => s.user);
  const pushToast = useToastStore((state) => state.push);
  const viewsQ = useTaskViews(id);
  const schemaQ = useDataManagerSchema(id);
  const createView = useCreateTaskView(id);
  const updateView = useUpdateTaskView(id);
  const deleteView = useDeleteTaskView(id);
  const [selectedKey, setSelectedKey] = useState<string>(requestedDataManagerViewKey(currentUrl));
  const draftOwner = `tasks:${id}:${user?.id ?? "anonymous"}:${selectedKey}`;
  const mutationOwnerRef = useRef(draftOwner);
  mutationOwnerRef.current = draftOwner;
  const { hasInvalidDraft, onDraftValidityChange } = useFilterDraftValidity(draftOwner);
  const [filterExpression, setFilterExpression] = useState<DataManagerFilterExpression>({});
  const [appliedFilterExpression, setAppliedFilterExpression] =
    useState<DataManagerFilterExpression>({});
  const [keyword, setKeyword] = useState("");
  const [keywordFlushKey, setKeywordFlushKey] = useState(0);
  const debouncedKeyword = useDebouncedValue(keyword, 250, keywordFlushKey);
  const [columns, setColumns] = useState<string[]>(DEFAULT_COLUMNS);
  const [sort, setSort] = useState<TaskSortItem[]>([
    { field: "task.created_at", direction: "asc" },
  ]);
  const [layout, setLayout] = useState<DataManagerLayout>(currentUrl.layout ?? "list");
  const [selectedTaskIds, setSelectedTaskIds] = useState<string[]>(() =>
    normalizeTaskSelection(currentUrl.selectedTasks),
  );
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(20);
  const [saveDialogOpen, setSaveDialogOpen] = useState(false);
  const [saveName, setSaveName] = useState("任务视图");
  const [saveVisibility, setSaveVisibility] = useState<"private" | "project">("private");
  const [selectedTask, setSelectedTask] = useState<SelectedTask | null>(null);
  const selectedTaskRef = useRef<SelectedTask | null>(null);
  selectedTaskRef.current = selectedTask;
  const resultsStartRef = useRef<HTMLDivElement>(null);
  const stickyHeaderRef = usePageTableHeader<HTMLTableSectionElement>(
    scrollContainerRef,
    resultsStartRef,
  );
  const [baselineSignature, setBaselineSignature] = useState("");
  const [pendingViewKey, setPendingViewKey] = useState<string | null>(null);
  const [pendingScope, setPendingScope] = useState<DataManagerEntityScope | null>(null);
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [analyticsOpen, setAnalyticsOpen] = useState(
    () => typeof window !== "undefined" && localStorage.getItem("dm-analytics-open") === "1",
  );
  const [viewsRailOpen, setViewsRailOpen] = useState(true);
  const [urlHydrated, setUrlHydrated] = useState(false);
  const lastWrittenUrlRef = useRef<string | null>(null);
  const pendingViewKeyRef = useRef<string | null>(null);
  const mountedRef = useRef(false);
  const skipUrlSyncRef = useRef(false);
  const projectAccountRef = useRef(`${id}:${user?.id ?? ""}`);
  const taskSelectionSignatureRef = useRef<string | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    const nextOwner = `${id}:${user?.id ?? ""}`;
    if (projectAccountRef.current === nextOwner) return;
    projectAccountRef.current = nextOwner;
    setSelectedTaskIds([]);
    setSelectedTask(null);
    setSearchParams(
      updateDataManagerUrl(searchParams, {
        ...currentUrl,
        selected: null,
        selectedTasks: null,
      }),
      { replace: true },
    );
  }, [currentUrl, id, searchParams, setSearchParams, user?.id]);

  const views = useMemo(() => viewsQ.data?.items ?? [], [viewsQ.data?.items]);
  const filterFields = useMemo(
    () =>
      (schemaQ.data?.filter_fields ?? FALLBACK_FILTER_FIELDS).filter(
        (field) => field.key !== "task.keyword",
      ),
    [schemaQ.data?.filter_fields],
  );
  const columnOptions = useMemo(
    () =>
      schemaQ.data?.columns ??
      COLUMN_OPTIONS.map((column) => ({
        ...column,
        group: "任务",
        default: DEFAULT_COLUMNS.includes(column.key),
        expensive: false,
        sortable: false,
        sort_field: null,
      })),
    [schemaQ.data?.columns],
  );
  const defaultColumns = useMemo(
    () => (schemaQ.data?.default_columns?.length ? schemaQ.data.default_columns : DEFAULT_COLUMNS),
    [schemaQ.data?.default_columns],
  );
  const fieldLabel = useMemo(
    () => new Map(filterFields.map((field) => [field.key, field.label])),
    [filterFields],
  );
  const selectedView = useMemo(() => findDataManagerView(views, selectedKey), [selectedKey, views]);

  useEffect(() => {
    if (lastWrittenUrlRef.current === searchParams.toString()) return;
    if (pendingViewKeyRef.current === selectedKey) return;
    const requestedKey = requestedDataManagerViewKey(currentUrl);
    if (requestedKey !== selectedKey) {
      setUrlHydrated(false);
      setSelectedKey(requestedKey);
    }
  }, [currentUrl.lens, currentUrl.view, searchParams, selectedKey]);

  useEffect(() => {
    if (!views.length) return;
    if (!selectedView) {
      const firstKey = resolveDataManagerViewKey(selectedKey, views);
      if (!firstKey) return;
      setUrlHydrated(false);
      setSelectedKey(firstKey);
      setSearchParams(
        updateDataManagerUrl(searchParams, {
          lens: "tasks",
          view: firstKey,
          query: currentUrl.query,
          filter: currentUrl.filter,
          sort: currentUrl.sort,
          columns: currentUrl.columns,
          selected: currentUrl.selected,
        }),
        { replace: true },
      );
    }
  }, [currentUrl, searchParams, selectedKey, selectedView, setSearchParams, views]);

  useEffect(() => {
    if (!selectedView) return;
    const url = currentUrl;
    const useUrl = shouldUseDataManagerUrlOverrides(currentUrl, searchParams, selectedKey);
    if (lastWrittenUrlRef.current === searchParams.toString()) {
      lastWrittenUrlRef.current = null;
      if (pendingViewKeyRef.current !== selectedKey) {
        setUrlHydrated(true);
        return;
      }
      pendingViewKeyRef.current = null;
    }
    const source = (useUrl && url.filter ? url.filter : selectedView.filter_json) as
      | DataManagerFilterExpression
      | Record<string, unknown>;
    const structureIssue = validateFilterStructure(source);
    const split = structureIssue
      ? { query: "", filter: source }
      : splitKeyword(collapseEmptyGroups(source));
    const nextKeyword = useUrl && searchParams.has("q") ? url.query : split.query;
    setKeyword(nextKeyword);
    setKeywordFlushKey((value) => value + 1);
    setFilterExpression(split.filter);
    setAppliedFilterExpression(split.filter);
    const allowedColumns = new Set(columnOptions.map((column) => column.key));
    const restoredColumns = normalizeDataManagerColumns(
      useUrl && url.columns?.length
        ? url.columns
        : selectedView.columns_json?.length
          ? selectedView.columns_json
          : defaultColumns,
    ).filter((column) => allowedColumns.has(column));
    const nextColumns = restoredColumns.length ? restoredColumns : defaultColumns;
    const nextSort = resolveDataManagerSort(
      useUrl ? url.sort : null,
      selectedView.sort_json,
      schemaQ.data?.sort_fields.map((field) => field.value) ?? [],
      schemaQ.data?.sort_fields[0]?.value ?? "task.created_at",
    );
    setColumns(nextColumns);
    setSort(nextSort);
    setLayout(useUrl ? (url.layout ?? "list") : "list");
    const restoredTaskIds = useUrl ? normalizeTaskSelection(url.selectedTasks) : [];
    setSelectedTaskIds(restoredTaskIds);
    taskSelectionSignatureRef.current = JSON.stringify({
      view: selectedKey,
      filter_json: combineKeyword(nextKeyword, split.filter),
    });
    setBaselineSignature(
      structureIssue
        ? ""
        : JSON.stringify({
            filter_json: combineKeyword(nextKeyword, split.filter),
            sort_json: nextSort,
            columns_json: nextColumns,
          }),
    );
    setUrlHydrated(true);
    skipUrlSyncRef.current = true;
    setPage(0);
  }, [
    columnOptions,
    currentUrl,
    defaultColumns,
    filterFields,
    searchParams,
    schemaQ.data?.sort_fields,
    selectedKey,
    selectedView,
  ]);

  const switchView = (key: string) => {
    const next = updateDataManagerUrl(searchParams, {
      section: "data",
      lens: "tasks",
      view: key,
      query: "",
      filter: null,
      sort: null,
      columns: null,
      selected: null,
      selectedTasks: null,
    });
    setUrlHydrated(false);
    pendingViewKeyRef.current = key;
    lastWrittenUrlRef.current = next.toString();
    setSelectedTaskIds([]);
    setSelectedKey(key);
    setSearchParams(next);
  };

  const expressionValid = useMemo(
    () => isExpressionValid(filterExpression, filterFields),
    [filterExpression, filterFields],
  );
  const filterStructureIssue = useMemo(
    () => validateFilterStructure(filterExpression),
    [filterExpression],
  );
  const filterReady = expressionValid && !hasInvalidDraft;
  useEffect(() => {
    if (expressionValid) setAppliedFilterExpression(filterExpression);
  }, [expressionValid, filterExpression]);
  const queryExpression = useMemo(
    () => (filterReady ? filterExpression : filterStructureIssue ? {} : appliedFilterExpression),
    [appliedFilterExpression, filterExpression, filterReady, filterStructureIssue],
  );
  const filterJson = useMemo(
    () => combineKeyword(debouncedKeyword, queryExpression),
    [debouncedKeyword, queryExpression],
  );
  const queryStateSignature = useMemo(
    () =>
      JSON.stringify({
        projectId: id,
        userId: user?.id,
        filter_json: filterJson,
        sort_json: sort,
        pageSize,
      }),
    [id, user?.id, filterJson, sort, pageSize],
  );
  const pageSignatureRef = useRef("");
  const pageForQuery = pageSignatureRef.current === queryStateSignature ? page : 0;
  useEffect(() => {
    if (pageSignatureRef.current === queryStateSignature) return;
    pageSignatureRef.current = queryStateSignature;
    setPage(0);
  }, [queryStateSignature]);
  // Query, sort, saved view or task page changes return the reader to the start
  // of the results on the Data page scroll owner. Loading more of the same page
  // keeps the current viewport position.
  useEffect(() => {
    const scroller = scrollContainerRef.current;
    const target = resultsStartRef.current;
    if (!scroller || !target) return;
    const top = Math.max(
      0,
      target.getBoundingClientRect().top -
        scroller.getBoundingClientRect().top +
        scroller.scrollTop -
        8,
    );
    if (scroller.scrollTop > top) scroller.scrollTo({ top });
  }, [queryStateSignature, page, scrollContainerRef]);
  const queryPayload = useMemo(
    () => ({
      filter_json: filterJson as Record<string, unknown>,
      sort_json: sort,
      columns_json:
        layout === "gallery"
          ? [
              ...new Set([
                ...columns,
                "annotation_count",
                "unresolved_issue_count",
                "comment_count",
              ]),
            ]
          : columns,
      limit: pageSize,
      offset: pageForQuery * pageSize,
    }),
    [columns, filterJson, layout, pageForQuery, pageSize, sort],
  );
  const queryReady = Boolean(
    selectedView &&
    schemaQ.data &&
    urlHydrated &&
    expressionValid &&
    sort.length &&
    sort.every((item) => schemaQ.data.sort_fields.some((field) => field.value === item.field)),
  );
  const tasksQ = useProjectTaskQuery(id, queryPayload, queryReady);
  const summaryQ = useDataManagerSummary(id, filterJson as Record<string, unknown>, queryReady);
  const selectedTaskOnPage = tasksQ.data?.items.find((task) => task.id === currentUrl.selected);
  const selectedTaskLookupQ = useTask(
    currentUrl.selected && !selectedTaskOnPage ? currentUrl.selected : "",
  );
  const selectedTaskIdForUrl =
    selectedTask?.id ??
    selectedTaskOnPage?.id ??
    (currentUrl.selected &&
    !selectedTaskOnPage &&
    (selectedTaskLookupQ.isLoading || selectedTaskLookupQ.data?.id === currentUrl.selected)
      ? currentUrl.selected
      : null);
  const currentSignature = useMemo(
    () => JSON.stringify({ filter_json: filterJson, sort_json: sort, columns_json: columns }),
    [columns, filterJson, sort],
  );
  const isDirty = Boolean(
    baselineSignature && (!filterReady || baselineSignature !== currentSignature),
  );
  useEffect(() => {
    onDirtyChange?.(isDirty);
  }, [isDirty, onDirtyChange]);
  const totalRef = useRef({ scope: queryStateSignature, total: 0 });
  if (tasksQ.data && !tasksQ.isPlaceholderData)
    totalRef.current = { scope: queryStateSignature, total: tasksQ.data.total };
  const total =
    tasksQ.data?.total ??
    (totalRef.current.scope === queryStateSignature ? totalRef.current.total : 0);
  const visibleTotal =
    summaryQ.data?.scope.visible_task_total ??
    views.find((view) => view.key === "all")?.task_count ??
    0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  useEffect(() => {
    if (tasksQ.data && !tasksQ.isFetching && page >= totalPages) setPage(totalPages - 1);
  }, [page, tasksQ.data, tasksQ.isFetching, totalPages]);
  const projectAccess = useProjectAccess(id);
  const canManageProject = projectAccess.hasCapability("project.manage");
  // Data Manager reads stay project.read; management actions need project.manage
  // and selected-task export needs export.annotations (reviewer, not annotator).
  const canManageProjectTasks = canManageProject;
  const canExportProjectTasks = projectAccess.hasCapability("export.annotations");
  const showProjectTaskActions = canManageProjectTasks || canExportProjectTasks;
  const canEditSelected = Boolean(
    selectedView?.id &&
    (selectedView.visibility === "private"
      ? selectedView.owner_id === user?.id || role === "super_admin"
      : canManageProject),
  );

  const selectionFilterSignatureRef = useRef<string | null>(null);
  useEffect(() => {
    if (!urlHydrated) return;
    if (selectionFilterSignatureRef.current === null) {
      selectionFilterSignatureRef.current = currentSignature;
      return;
    }
    if (selectionFilterSignatureRef.current !== currentSignature) {
      selectionFilterSignatureRef.current = currentSignature;
      setSelectedTask(null);
    }
  }, [currentSignature, urlHydrated]);

  const taskSelectionSignature = useMemo(
    () => JSON.stringify({ view: selectedKey, filter_json: filterJson }),
    [filterJson, selectedKey],
  );
  useEffect(() => {
    if (!urlHydrated) return;
    if (taskSelectionSignatureRef.current === null) {
      taskSelectionSignatureRef.current = taskSelectionSignature;
      return;
    }
    if (taskSelectionSignatureRef.current !== taskSelectionSignature) {
      taskSelectionSignatureRef.current = taskSelectionSignature;
      setSelectedTaskIds([]);
    }
  }, [taskSelectionSignature, urlHydrated]);

  const effectiveSelectedTaskIds = useMemo(
    () => (taskSelectionSignatureRef.current === taskSelectionSignature ? selectedTaskIds : []),
    [selectedTaskIds, taskSelectionSignature],
  );

  useEffect(() => {
    if (!urlHydrated || skipUrlSyncRef.current || !filterReady) {
      skipUrlSyncRef.current = false;
      return;
    }
    const next = updateDataManagerUrl(searchParams, {
      section: "data",
      lens: "tasks",
      view: selectedKey,
      query: keyword,
      filter: isEmptyFilter(queryExpression) ? {} : (queryExpression as Record<string, unknown>),
      sort,
      columns,
      selected: selectedTaskIdForUrl,
      selectedTasks: effectiveSelectedTaskIds,
      layout,
    });
    if (next.toString() !== searchParams.toString()) {
      lastWrittenUrlRef.current = next.toString();
      setSearchParams(next, { replace: true });
    }
  }, [
    columns,
    queryExpression,
    keyword,
    searchParams,
    selectedKey,
    selectedTaskIdForUrl,
    effectiveSelectedTaskIds,
    setSearchParams,
    sort,
    filterReady,
    layout,
    urlHydrated,
  ]);

  useEffect(() => {
    if (!currentUrl.selected) {
      setSelectedTask(null);
      return;
    }
    const restored = selectedTaskOnPage;
    if (restored) {
      setSelectedTask(restored);
      return;
    }
    const fetched = selectedTaskLookupQ.data;
    if (fetched?.id === currentUrl.selected && fetched.project_id === project.id) {
      setSelectedTask(fetched);
      return;
    }
    if (
      selectedTaskRef.current?.id !== currentUrl.selected ||
      selectedTaskRef.current.project_id !== project.id
    ) {
      setSelectedTask(null);
    }
  }, [currentUrl.selected, project.id, selectedTaskLookupQ.data, selectedTaskOnPage]);

  const selectedTaskDetails =
    selectedTask &&
    selectedTask.project_id === project.id &&
    (!currentUrl.selected || selectedTask.id === currentUrl.selected)
      ? selectedTask
      : selectedTaskLookupQ.data?.id === currentUrl.selected &&
          selectedTaskLookupQ.data.project_id === project.id
        ? selectedTaskLookupQ.data
        : null;

  if (schemaQ.isError)
    return (
      <div role="alert" className="p-6 text-center text-sm text-destructive">
        <p>无法加载 Data Manager 筛选字段。</p>
        <Button size="sm" variant="ghost" className="mt-3" onClick={() => void schemaQ.refetch()}>
          <Icon name="refresh" size={12} />
          重试
        </Button>
      </div>
    );

  const saveCurrent = async () => {
    if (!filterReady) {
      pushToast({ msg: "请先完成筛选条件", kind: "warning" });
      return;
    }
    const ownerAtStart = draftOwner;
    const payload = {
      name: selectedView?.name ?? "任务视图",
      visibility: selectedView?.visibility ?? "private",
      filter_json: filterJson as Record<string, unknown>,
      sort_json: sort,
      columns_json: columns,
    };
    if (canEditSelected && selectedView?.id) {
      try {
        await updateView.mutateAsync({ viewId: selectedView.id, payload });
        if (!mountedRef.current || mutationOwnerRef.current !== ownerAtStart) return;
        setBaselineSignature(currentSignature);
        pushToast({ msg: "视图已保存", kind: "success" });
      } catch {
        if (!mountedRef.current || mutationOwnerRef.current !== ownerAtStart) return;
        pushToast({ msg: "无法保存视图", sub: "请检查网络后重试", kind: "error" });
      }
      return;
    }
    setSaveName(selectedView ? `${selectedView.name} 副本` : "任务视图");
    setSaveVisibility("private");
    setSaveDialogOpen(true);
  };

  const createSavedView = async () => {
    const name = saveName.trim();
    if (!name || !filterReady) return;
    const ownerAtStart = draftOwner;
    try {
      const created = await createView.mutateAsync({
        name,
        visibility: saveVisibility,
        entity_scope: "tasks",
        filter_json: filterJson as Record<string, unknown>,
        sort_json: sort,
        columns_json: columns,
      });
      if (!mountedRef.current || mutationOwnerRef.current !== ownerAtStart) return;
      await viewsQ.refetch();
      if (!mountedRef.current || mutationOwnerRef.current !== ownerAtStart) return;
      switchView(`saved:${created.id}`);
      setSaveDialogOpen(false);
      pushToast({ msg: "视图已创建", kind: "success" });
    } catch {
      if (!mountedRef.current || mutationOwnerRef.current !== ownerAtStart) return;
      pushToast({ msg: "无法创建视图", sub: "名称可能已存在", kind: "error" });
    }
  };

  const removeCurrent = async () => {
    if (!selectedView?.id || !canEditSelected) return;
    try {
      await deleteView.mutateAsync(selectedView.id);
      switchView("builtin:all");
      pushToast({ msg: "视图已删除", kind: "success" });
    } catch {
      pushToast({ msg: "无法删除视图", kind: "error" });
    }
  };

  const visibleColumnSet = new Set(columns);
  const expressionRuleEntries = isFilterGroup(filterExpression)
    ? (filterExpression.rules
        .map((rule, index) => ({ rule, path: [index] }))
        .filter(({ rule }) => isFilterRule(rule)) as Array<{
        rule: TaskFilterRule;
        path: number[];
      }>)
    : isFilterRule(filterExpression)
      ? [{ rule: filterExpression, path: [] }]
      : [];
  const conjunctEntries =
    isFilterGroup(filterExpression) && filterExpression.op === "or" ? [] : expressionRuleEntries;
  const toggleQuickRule = (rule: RuleChipDraft) => {
    const existing = conjunctEntries.find(
      ({ rule: item }) =>
        item.field === rule.field && item.op === rule.op && String(item.value ?? "") === rule.value,
    );
    if (existing) {
      setFilterExpression(removeAtPath(filterExpression, existing.path));
      return;
    }
    const field = filterFields.find((item) => item.key === rule.field);
    if (!field) return;
    const parsed = parseFilterValue(field, rule.op, rule.value);
    if (!parsed.ok) return;
    setFilterExpression((previous) => {
      const next = appendRule(previous, field);
      const addedPath = isFilterGroup(next) ? [next.rules.length - 1] : [];
      return updateRuleAtPath(next, addedPath, (item) => ({
        ...item,
        op: rule.op,
        value: parsed.value,
      }));
    });
  };
  const toggleAnalytics = () => {
    setAnalyticsOpen((value) => {
      const next = !value;
      localStorage.setItem("dm-analytics-open", next ? "1" : "0");
      return next;
    });
  };
  const quickFilters: DataManagerQuickFilter[] = [
    {
      key: "low-confidence",
      label: "低置信",
      active: conjunctEntries.some(
        ({ rule }) =>
          rule.field === "ai.low_confidence_prediction_shape_count" &&
          rule.op === "gt" &&
          String(rule.value) === "0",
      ),
      onClick: () =>
        toggleQuickRule({
          field: "ai.low_confidence_prediction_shape_count",
          op: "gt",
          value: "0",
        }),
    },
    {
      key: "feedback",
      label: "有未解决问题",
      active: conjunctEntries.some(
        ({ rule }) =>
          rule.field === "feedback.unresolved_count" &&
          rule.op === "gt" &&
          String(rule.value) === "0",
      ),
      onClick: () => toggleQuickRule({ field: "feedback.unresolved_count", op: "gt", value: "0" }),
    },
    {
      key: "manual",
      label: "人工标注",
      active: conjunctEntries.some(
        ({ rule }) =>
          rule.field === "annotation.source" && rule.op === "eq" && String(rule.value) === "manual",
      ),
      onClick: () => toggleQuickRule({ field: "annotation.source", op: "eq", value: "manual" }),
    },
  ];
  const topRuleEntries = expressionRuleEntries;
  const filterChips: DataManagerFilterChip[] = topRuleEntries.map(({ rule, path }) => {
    const field = filterFields.find((item) => item.key === rule.field);
    const draft = formatFilterDraft(rule.value, field, rule.op);
    return {
      id: `${path.join(".")}:${rule.field}`,
      onRemove: () => setFilterExpression(removeAtPath(filterExpression, path)),
      label: fieldLabel.get(rule.field) ?? rule.field,
      value: ruleValueLabel(
        { field: rule.field, op: rule.op, value: rule.value === null ? "null" : draft },
        filterFields,
      ),
      editor: (
        <div className="flex flex-col gap-2">
          <select
            className={FIELD_CLASS}
            value={rule.op}
            onChange={(event) => {
              setFilterExpression(
                updateRuleAtPath(filterExpression, path, (item) => ({
                  ...item,
                  op: event.target.value as TaskFilterOp,
                })),
              );
            }}
          >
            {(field?.operators ?? [rule.op]).map((operator) => (
              <option key={operator} value={operator}>
                {filterOperatorLabel(operator)}
              </option>
            ))}
          </select>
          {field ? (
            <FilterValueEditor
              field={field}
              operator={rule.op}
              appliedValue={rule.value}
              editorId={`${draftOwner}:chip:${path.join(".") || "root"}`}
              onDraftValidityChange={onDraftValidityChange}
              onCommit={(value) =>
                setFilterExpression(
                  updateRuleAtPath(filterExpression, path, (item) => ({ ...item, value })),
                )
              }
            />
          ) : (
            <div role="alert" className="text-xs text-destructive">
              当前字段不在 schema 中，无法执行条件
            </div>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setFilterExpression(removeAtPath(filterExpression, path))}
          >
            <Icon name="trash" size={12} />
            移除条件
          </Button>
        </div>
      ),
    };
  });

  const tasks = tasksQ.data?.items ?? [];
  const pageTaskIds = tasks.map((task) => task.id);
  const pageSelectionComplete = Boolean(
    pageTaskIds.length && pageTaskIds.every((taskId) => effectiveSelectedTaskIds.includes(taskId)),
  );
  const toggleTaskSelection = (taskId: string, checked: boolean) => {
    setSelectedTaskIds((current) => {
      if (checked) {
        if (current.includes(taskId)) return current;
        if (current.length >= MAX_SELECTED_TASKS) {
          pushToast({ msg: `最多选择 ${MAX_SELECTED_TASKS} 个任务`, kind: "warning" });
          return current;
        }
        return [...current, taskId];
      }
      return current.filter((id) => id !== taskId);
    });
  };
  const toggleCurrentPageSelection = (checked: boolean) => {
    if (!checked) {
      setSelectedTaskIds((current) => current.filter((id) => !pageTaskIds.includes(id)));
      return;
    }
    const additions = pageTaskIds.filter((id) => !effectiveSelectedTaskIds.includes(id));
    if (effectiveSelectedTaskIds.length + additions.length > MAX_SELECTED_TASKS) {
      pushToast({ msg: `最多选择 ${MAX_SELECTED_TASKS} 个任务`, kind: "warning" });
      return;
    }
    setSelectedTaskIds((current) => normalizeTaskSelection([...current, ...additions]));
  };
  const viewGroups = [
    { key: "builtin", label: "内置视图", items: views.filter((view) => view.builtin) },
    {
      key: "project",
      label: "项目共享",
      items: views.filter((view) => !view.builtin && view.visibility === "project"),
    },
    {
      key: "private",
      label: "我的视图",
      items: views.filter((view) => !view.builtin && view.visibility !== "project"),
    },
  ].filter((group) => group.items.length);

  const hasActiveFilters = !isEmptyFilter(filterExpression) || Boolean(debouncedKeyword || keyword);

  return (
    <div className="text-foreground">
      <DataManagerLensTabs
        scope="tasks"
        availableScopes={availableScopes}
        onScopeChange={(nextScope) => {
          if (nextScope === "tasks") return;
          if (isDirty) setPendingScope(nextScope);
          else onScopeChange(nextScope);
        }}
      >
        <div className="flex flex-col gap-2">
          <header className="flex shrink-0 items-center justify-end gap-4 max-md:flex-col max-md:items-stretch">
            <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground max-sm:flex-wrap">
              {!!urlState.issues.length && (
                <div role="alert" className="mt-1 text-xs text-status-caution">
                  URL 状态无法完整恢复，已使用安全默认值。
                </div>
              )}
              <span>{visibleTotal.toLocaleString()} 可见任务</span>
              <span aria-hidden="true">·</span>
              <span>{total.toLocaleString()} 当前匹配</span>
              <span aria-hidden="true">·</span>
              <span>{views.length.toLocaleString()} 个视图</span>
            </div>
            <DataManagerViewActions
              analyticsOpen={analyticsOpen}
              onToggleAnalytics={toggleAnalytics}
              onRefresh={() => {
                if (!queryReady || !filterReady) return;
                tasksQ.refetch();
                summaryQ.refetch();
                viewsQ.refetch();
              }}
              refreshDisabled={
                !queryReady || !filterReady || tasksQ.isFetching || summaryQ.isFetching
              }
              onSave={saveCurrent}
              saveDisabled={!filterReady || createView.isPending || updateView.isPending}
            />
          </header>

          <DataManagerSummaryStrip
            summary={summaryQ.data}
            isLoading={summaryQ.isLoading}
            onDrill={(rule) => {
              if (!filterFields.some((item) => item.key === rule.field)) return;
              toggleQuickRule({
                field: rule.field,
                op: rule.op as TaskFilterOp,
                value: rule.value,
              });
            }}
          />

          {analyticsOpen && (
            <DataManagerAnalyticsPanel
              scope="tasks"
              summary={summaryQ.data}
              isLoading={summaryQ.isLoading}
              fields={filterFields}
              onSelect={(field, value) => {
                if (!filterFields.some((item) => item.key === field)) return;
                toggleQuickRule({ field, op: "eq" as const, value });
              }}
            />
          )}

          <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-3 max-lg:grid-cols-1">
            <aside
              className={cn(
                "min-h-0 w-[210px] overflow-y-auto rounded-md border border-border bg-card p-2 max-lg:hidden",
                !viewsRailOpen && "w-12",
              )}
              aria-label="已保存的数据视图"
            >
              <div
                className={cn(
                  "flex items-center gap-1 px-1 pb-2",
                  !viewsRailOpen && "justify-center",
                )}
              >
                <button
                  type="button"
                  aria-label={viewsRailOpen ? "折叠视图栏" : "展开视图栏"}
                  aria-expanded={viewsRailOpen}
                  className="inline-flex size-7 items-center justify-center rounded-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() => setViewsRailOpen((open) => !open)}
                >
                  <Icon name="panelLeft" size={14} />
                </button>
                {viewsRailOpen && (
                  <span className="text-xs font-semibold text-muted-foreground">视图</span>
                )}
              </div>
              {viewsRailOpen && (
                <div className="mb-2 flex flex-col gap-2">
                  {viewGroups.map((group) => (
                    <details key={group.key} open className="group">
                      <summary className="cursor-pointer px-1 py-1 text-2xs font-semibold text-muted-foreground marker:text-muted-foreground">
                        {group.label}
                      </summary>
                      <div className="mt-0.5 flex flex-col gap-0.5">
                        {group.items.map((view) => {
                          const key = dataManagerViewKey(view);
                          const active = key === selectedKey;
                          return (
                            <button
                              key={key}
                              type="button"
                              className={cn(
                                "flex min-h-[34px] w-full cursor-pointer appearance-none items-center justify-between gap-2 rounded-sm border border-transparent bg-transparent px-2 py-1.5 text-left text-sm text-muted-foreground hover:bg-muted hover:text-foreground [&>span:first-child]:min-w-0 [&>span:first-child]:overflow-hidden [&>span:first-child]:text-ellipsis [&>span:first-child]:whitespace-nowrap",
                                active && "border-border bg-muted text-foreground",
                              )}
                              onClick={() => {
                                if (key === selectedKey) return;
                                if (isDirty) setPendingViewKey(key);
                                else switchView(key);
                              }}
                            >
                              <span>{view.name}</span>
                              <Badge
                                variant={
                                  view.builtin
                                    ? "outline"
                                    : view.visibility === "project"
                                      ? "accent"
                                      : "default"
                                }
                              >
                                {view.invalid_fields.length ? "失效" : (view.task_count ?? "—")}
                              </Badge>
                            </button>
                          );
                        })}
                      </div>
                    </details>
                  ))}
                </div>
              )}
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setDeleteConfirmOpen(true)}
                disabled={!canEditSelected || deleteView.isPending}
                className={cn(
                  "w-full justify-start text-muted-foreground",
                  !viewsRailOpen && "px-0",
                )}
                aria-label="删除当前视图"
              >
                <Icon name="trash" size={12} />
                {viewsRailOpen && "删除"}
              </Button>
            </aside>

            <div className="flex min-w-0 flex-col gap-2">
              <section className="flex shrink-0 flex-col gap-2 rounded-md border border-border bg-card p-2.5">
                <div className="flex items-center justify-between gap-3 px-0.5 pb-0.5">
                  <div>
                    <div className="text-sm font-semibold">{selectedView?.name ?? "任务视图"}</div>
                    <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                      {selectedView?.builtin
                        ? "内置视图"
                        : selectedView?.visibility === "project"
                          ? "项目共享"
                          : "私有视图"}
                      <span>·</span>
                      {total.toLocaleString()} 条匹配
                    </div>
                  </div>
                  <Badge variant={isDirty ? "warning" : canEditSelected ? "accent" : "outline"}>
                    {isDirty ? "未保存" : canEditSelected ? "可编辑" : "只读"}
                  </Badge>
                </div>
                <div className="flex gap-2 max-sm:flex-col">
                  <Select
                    value={selectedKey}
                    onValueChange={(key) => {
                      if (key === selectedKey) return;
                      if (isDirty) setPendingViewKey(key);
                      else switchView(key);
                    }}
                  >
                    <SelectTrigger aria-label="选择任务视图" className="hidden w-44 max-lg:flex">
                      <SelectValue placeholder="选择视图" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {views.map((view) => {
                          const key = dataManagerViewKey(view);
                          return (
                            <SelectItem key={key} value={key}>
                              {view.name}
                            </SelectItem>
                          );
                        })}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <div className="relative min-w-0 flex-1">
                    <Icon
                      name="search"
                      size={14}
                      className="pointer-events-none absolute top-1/2 left-2.5 -translate-y-1/2 text-muted-foreground"
                    />
                    <Input
                      value={keyword}
                      onChange={(event) => setKeyword(event.target.value)}
                      placeholder="搜索任务编号或文件名"
                      aria-label="搜索任务编号或文件名"
                      className="h-9 pl-8"
                    />
                  </div>
                  <Select
                    value={sort[0]?.field ?? "task.created_at"}
                    onValueChange={(field) => {
                      setSort([{ field, direction: sort[0]?.direction ?? "asc" }]);
                      setPage(0);
                    }}
                  >
                    <SelectTrigger aria-label="任务排序字段" className="w-40">
                      <SelectValue placeholder="排序字段" />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectGroup>
                        {(schemaQ.data?.sort_fields ?? []).map((field) => (
                          <SelectItem key={field.value} value={field.value}>
                            {field.label}
                          </SelectItem>
                        ))}
                      </SelectGroup>
                    </SelectContent>
                  </Select>
                  <Button
                    onClick={() => {
                      const current = sort[0] ?? {
                        field: "task.created_at",
                        direction: "asc" as const,
                      };
                      setSort([
                        { ...current, direction: current.direction === "asc" ? "desc" : "asc" },
                      ]);
                      setPage(0);
                    }}
                  >
                    {sort[0]?.direction === "desc" ? "降序" : "升序"}
                  </Button>
                  <div
                    role="group"
                    aria-label="结果布局"
                    className="flex shrink-0 rounded-sm border border-border"
                  >
                    <button
                      type="button"
                      aria-label="列表视图"
                      aria-pressed={layout === "list"}
                      className={cn(
                        "inline-flex size-9 items-center justify-center rounded-l-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:z-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        layout === "list" && "bg-muted text-foreground",
                      )}
                      onClick={() => setLayout("list")}
                    >
                      <Icon name="list" size={14} />
                    </button>
                    <button
                      type="button"
                      aria-label="画廊视图"
                      aria-pressed={layout === "gallery"}
                      className={cn(
                        "inline-flex size-9 items-center justify-center rounded-r-sm text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:z-base focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                        layout === "gallery" && "bg-muted text-foreground",
                      )}
                      onClick={() => setLayout("gallery")}
                    >
                      <Icon name="grid" size={14} />
                    </button>
                  </div>
                  <Popover>
                    <PopoverTrigger asChild>
                      <Button>列设置</Button>
                    </PopoverTrigger>
                    <PopoverContent align="end" className="w-72">
                      <div className="mb-2 text-sm font-medium">显示列</div>
                      <div className="max-h-80 overflow-y-auto">
                        {columnOptions.map((column) => (
                          <label
                            key={column.key}
                            className="flex min-h-8 items-center gap-2 text-xs"
                          >
                            <input
                              type="checkbox"
                              checked={visibleColumnSet.has(column.key)}
                              disabled={column.key === "display_id"}
                              onChange={(event) =>
                                setColumns(
                                  event.target.checked
                                    ? [...columns, column.key]
                                    : columns.filter((item) => item !== column.key),
                                )
                              }
                            />
                            <span>{column.label}</span>
                          </label>
                        ))}
                      </div>
                    </PopoverContent>
                  </Popover>
                </div>
                <DataManagerFilterBar
                  fields={filterFields}
                  chips={filterChips}
                  quickFilters={quickFilters}
                  hasConditions={!isEmptyFilter(filterExpression)}
                  onAdd={(field) => setFilterExpression((previous) => appendRule(previous, field))}
                  onClear={() => setFilterExpression({})}
                />
                {isFilterGroup(filterExpression) &&
                  (!expressionValid ||
                    filterExpression.op === "or" ||
                    hasNestedGroups(filterExpression)) && (
                    <DataManagerExpressionEditor
                      expression={filterExpression}
                      fields={filterFields}
                      onChange={setFilterExpression}
                      editorId={`${draftOwner}:group`}
                      onValidityChange={onDraftValidityChange}
                    />
                  )}
                {!filterReady && (
                  <div role="alert" className="text-xs text-destructive">
                    当前筛选包含未完成或 schema 中不存在的条件，完成编辑后才会查询。
                  </div>
                )}
                <div
                  data-dm-task-actions
                  data-dm-task-actions-disabled={!filterReady ? "true" : "false"}
                  className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border/80 bg-card/95 p-2.5 text-xs shadow-xs backdrop-blur-md transition-colors"
                >
                  <span className="text-muted-foreground">
                    {effectiveSelectedTaskIds.length
                      ? `已选择 ${effectiveSelectedTaskIds.length} / ${MAX_SELECTED_TASKS} 个任务`
                      : "可勾选任务进行批量操作"}
                  </span>
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={effectiveSelectedTaskIds.length === 0}
                    onClick={() => setSelectedTaskIds([])}
                    aria-label="清除任务选择"
                    className={cn(
                      effectiveSelectedTaskIds.length === 0 && "invisible pointer-events-none",
                    )}
                  >
                    清除选择
                  </Button>
                  {!filterReady && (
                    <span className="text-status-danger">完成筛选条件后才能执行批量操作</span>
                  )}
                  {showProjectTaskActions && (
                    <div className="basis-full">
                      <DataManagerTaskActions
                        key={`${id}:${user?.id ?? "anonymous"}`}
                        projectId={id}
                        taskIds={filterReady ? effectiveSelectedTaskIds : []}
                        canManage={canManageProjectTasks}
                        canExport={canExportProjectTasks}
                        onCompleted={() => {
                          setSelectedTaskIds([]);
                          void tasksQ.refetch();
                          void summaryQ.refetch();
                          void viewsQ.refetch();
                        }}
                      />
                    </div>
                  )}
                </div>
              </section>

              <div
                ref={resultsStartRef}
                // Contain wide columns without creating a short vertical scroller.
                className="w-full min-w-0 max-w-full min-h-[280px] overflow-x-auto rounded-md border border-border bg-card shadow-sm"
              >
                {layout === "gallery" ? (
                  <div
                    role="region"
                    aria-label="任务画廊"
                    className="grid gap-3 p-3 sm:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4"
                  >
                    {tasksQ.isLoading &&
                      Array.from({ length: 8 }, (_, index) => (
                        <Skeleton key={index} className="h-48 rounded-md" />
                      ))}
                    {tasksQ.isError && (
                      <div
                        role="alert"
                        className="col-span-full p-8 text-center text-sm text-destructive"
                      >
                        无法加载任务，请刷新重试
                      </div>
                    )}
                    {!tasksQ.isLoading && !tasksQ.isError && !tasks.length && (
                      <div className="col-span-full p-8 text-center text-sm text-muted-foreground">
                        {hasActiveFilters ? (
                          <div
                            data-testid="data-manager-filter-empty"
                            className="flex flex-col items-center justify-center p-6 text-center"
                          >
                            <div className="mb-2 flex size-10 items-center justify-center rounded-full bg-muted text-muted-foreground">
                              <Icon name="filter" size={18} />
                            </div>
                            <div className="font-semibold text-foreground">未找到匹配任务</div>
                            <p className="mt-1 max-w-sm text-xs text-muted-foreground">
                              当前筛选条件未匹配到任何数据，可重置筛选或调整查询关键词。
                            </p>
                            <Button
                              variant="default"
                              size="sm"
                              className="mt-3 inline-flex items-center gap-1.5"
                              onClick={() => {
                                setFilterExpression({});
                                setKeyword("");
                                setPage(0);
                              }}
                            >
                              <Icon name="rotate-ccw" size={12} />
                              重置所有筛选
                            </Button>
                          </div>
                        ) : (
                          "无匹配任务"
                        )}
                      </div>
                    )}
                    {tasks.map((task) => {
                      const taskSelected = effectiveSelectedTaskIds.includes(task.id);
                      return (
                        <article
                          key={task.id}
                          tabIndex={0}
                          className={cn(
                            "group cursor-pointer overflow-hidden rounded-md border border-border bg-card transition-colors hover:border-ring focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                            taskSelected && "border-primary",
                          )}
                          onClick={() => setSelectedTask(task)}
                          onKeyDown={(event) => {
                            if (event.target !== event.currentTarget) return;
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              setSelectedTask(task);
                            }
                          }}
                        >
                          <div className="relative flex h-32 items-center justify-center overflow-hidden bg-muted">
                            <TaskPreview task={task} width={160} height={128} />
                            <label
                              className="absolute top-2 left-2 rounded-sm bg-background/85 p-1.5 shadow-sm"
                              onClick={(event) => event.stopPropagation()}
                            >
                              <input
                                type="checkbox"
                                checked={taskSelected}
                                disabled={!filterReady}
                                aria-label={`选择任务 ${task.display_id}`}
                                onClick={(event) => event.stopPropagation()}
                                onChange={(event) =>
                                  toggleTaskSelection(task.id, event.target.checked)
                                }
                              />
                            </label>
                          </div>
                          <div className="flex items-start justify-between gap-2 p-3">
                            <div className="min-w-0">
                              <div
                                className="truncate font-mono text-sm font-medium"
                                title={task.display_id}
                              >
                                {task.display_id}
                              </div>
                              <div
                                className="mt-1 truncate text-xs text-muted-foreground"
                                title={task.file_name}
                              >
                                {task.file_name || "未命名文件"}
                              </div>
                            </div>
                            <Badge
                              variant={
                                task.status === "completed"
                                  ? "success"
                                  : task.status === "review"
                                    ? "warning"
                                    : "outline"
                              }
                            >
                              {statusLabel(task.status)}
                            </Badge>
                          </div>
                          <div className="flex items-center gap-3 border-t border-border px-3 py-2 text-2xs text-muted-foreground">
                            <span>{task.annotation_count.toLocaleString()} 标注</span>
                            <span>{task.unresolved_issue_count.toLocaleString()} 未解决问题</span>
                            <span>{task.comment_count.toLocaleString()} 评论</span>
                            {(task.effective_assignee ?? task.assignee)?.name && (
                              <span className="truncate">
                                {(task.effective_assignee ?? task.assignee)?.name}
                              </span>
                            )}
                          </div>
                        </article>
                      );
                    })}
                  </div>
                ) : (
                  <table
                    className={cn(
                      listStyles.table,
                      "min-w-[1040px] table-fixed [&_td]:overflow-hidden [&_td]:text-ellipsis [&_td]:whitespace-nowrap [&_th]:overflow-hidden [&_th]:text-ellipsis [&_th:first-child]:w-[58px] [&_td:first-child]:w-[58px]",
                    )}
                  >
                    <thead ref={stickyHeaderRef} className="relative z-base">
                      <tr>
                        <th>
                          <input
                            type="checkbox"
                            checked={pageSelectionComplete}
                            disabled={!pageTaskIds.length || !filterReady}
                            aria-label="选择当前页任务"
                            onChange={(event) => toggleCurrentPageSelection(event.target.checked)}
                          />
                        </th>
                        <th aria-label="任务预览" className="w-[68px]">
                          预览
                        </th>
                        {columns.map((column) => (
                          <th
                            key={column}
                            className={cn(
                              column === "display_id" && "w-[130px]",
                              column === "file_name" && "min-w-[240px]",
                            )}
                          >
                            {columnOptions.find((item) => item.key === column)?.label ?? column}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {tasks.map((task) => {
                        const taskSelected = effectiveSelectedTaskIds.includes(task.id);
                        return (
                          <tr
                            key={task.id}
                            tabIndex={0}
                            aria-selected={taskSelected}
                            className={cn(
                              "cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                              taskSelected && "bg-muted",
                            )}
                            onClick={() => setSelectedTask(task)}
                            onKeyDown={(event) => {
                              if (event.target !== event.currentTarget) return;
                              if (event.key === "Enter" || event.key === " ") {
                                event.preventDefault();
                                setSelectedTask(task);
                              }
                            }}
                          >
                            <td className="w-[68px]">
                              <input
                                type="checkbox"
                                checked={taskSelected}
                                disabled={!filterReady}
                                aria-label={`选择任务 ${task.display_id}`}
                                onClick={(event) => event.stopPropagation()}
                                onChange={(event) =>
                                  toggleTaskSelection(task.id, event.target.checked)
                                }
                              />
                            </td>
                            <td>
                              <TaskPreview task={task} width={44} height={36} />
                            </td>
                            {columns.map((column) => (
                              <td
                                key={`${task.id}-${column}`}
                                title={column === "file_name" ? task.file_name : undefined}
                              >
                                {renderCell(task, column)}
                              </td>
                            ))}
                          </tr>
                        );
                      })}
                      {tasksQ.isLoading &&
                        Array.from({ length: 6 }, (_, rowIndex) => (
                          <tr key={`loading-${rowIndex}`}>
                            <td />
                            <td>
                              <Skeleton className="h-9 w-11" />
                            </td>
                            {columns.map((column) => (
                              <td key={`loading-${rowIndex}-${column}`}>
                                <Skeleton className="h-4 w-full" />
                              </td>
                            ))}
                          </tr>
                        ))}
                      {tasksQ.isError && (
                        <tr>
                          <td
                            colSpan={Math.max(2, columns.length + 2)}
                            className="py-16 text-center text-destructive"
                          >
                            无法加载任务，请刷新重试
                          </td>
                        </tr>
                      )}
                      {!tasksQ.isLoading && !tasksQ.isError && !tasks.length && (
                        <tr>
                          <td
                            colSpan={Math.max(2, columns.length + 2)}
                            className="py-12 text-center text-muted-foreground"
                          >
                            {hasActiveFilters ? (
                              <div
                                data-testid="data-manager-filter-empty"
                                className="flex flex-col items-center justify-center py-4 text-center"
                              >
                                <div className="mb-2 flex size-10 items-center justify-center rounded-full bg-muted text-muted-foreground">
                                  <Icon name="filter" size={18} />
                                </div>
                                <div className="font-semibold text-foreground">未找到匹配任务</div>
                                <p className="mt-1 max-w-sm text-xs text-muted-foreground">
                                  当前筛选条件未匹配到任何数据，可重置筛选或调整查询关键词。
                                </p>
                                <Button
                                  variant="default"
                                  size="sm"
                                  className="mt-3 inline-flex items-center gap-1.5"
                                  onClick={() => {
                                    setFilterExpression({});
                                    setKeyword("");
                                    setPage(0);
                                  }}
                                >
                                  <Icon name="rotate-ccw" size={12} />
                                  重置所有筛选
                                </Button>
                              </div>
                            ) : (
                              "无匹配任务"
                            )}
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                )}
              </div>

              <footer className="flex shrink-0 flex-col gap-2 text-xs text-muted-foreground">
                <div>
                  {[
                    ...(debouncedKeyword ? [`关键词 “${debouncedKeyword}”`] : []),
                    ...expressionRuleEntries
                      .filter(
                        ({ rule }) =>
                          rule.value !== undefined || ["exists", "missing"].includes(rule.op),
                      )
                      .map(
                        ({ rule }) =>
                          `${fieldLabel.get(rule.field) ?? rule.field} ${filterOperatorLabel(rule.op)}`,
                      ),
                  ].join(" / ") || "全部任务"}
                </div>
                <ListPagination
                  label="任务"
                  page={pageForQuery}
                  pageSize={pageSize}
                  total={total}
                  loading={tasksQ.isFetching}
                  onPageChange={setPage}
                  onPageSizeChange={(size) => {
                    setPageSize(size);
                    setPage(0);
                  }}
                  className="w-full"
                />
              </footer>
            </div>
          </div>
        </div>
        <Dialog open={saveDialogOpen} onOpenChange={setSaveDialogOpen}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>保存任务视图</DialogTitle>
              <DialogDescription>保存当前搜索、筛选、排序和显示列。</DialogDescription>
            </DialogHeader>
            <div className="flex flex-col gap-3">
              <label className="flex flex-col gap-1.5 text-sm">
                视图名称
                <Input
                  value={saveName}
                  onChange={(event) => setSaveName(event.target.value)}
                  autoFocus
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm">
                可见性
                <select
                  className={FIELD_CLASS}
                  value={saveVisibility}
                  onChange={(event) =>
                    setSaveVisibility(event.target.value as "private" | "project")
                  }
                >
                  <option value="private">仅自己</option>
                  {canManageProject && <option value="project">项目共享</option>}
                </select>
              </label>
            </div>
            <DialogFooter>
              <Button onClick={() => setSaveDialogOpen(false)}>取消</Button>
              <Button
                variant="primary"
                onClick={createSavedView}
                disabled={!saveName.trim() || !filterReady || createView.isPending}
              >
                保存
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <TaskMatchesSheet
          projectId={id}
          task={selectedTaskDetails}
          filterJson={filterJson as Record<string, unknown>}
          open={Boolean(selectedTaskDetails)}
          onOpenChange={(open) => {
            if (open) return;
            setSelectedTask(null);
            setSearchParams(updateDataManagerUrl(searchParams, { ...currentUrl, selected: null }), {
              replace: true,
            });
          }}
        />
        <AlertDialog
          open={Boolean(pendingViewKey || pendingScope)}
          onOpenChange={(open) => {
            if (!open) {
              setPendingViewKey(null);
              setPendingScope(null);
            }
          }}
        >
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>放弃未保存的视图修改？</AlertDialogTitle>
              <AlertDialogDescription>
                当前搜索、筛选、排序或显示列尚未保存。切换视图会丢弃这些修改。
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>继续编辑</AlertDialogCancel>
              <AlertDialogAction
                onClick={() => {
                  if (pendingViewKey) switchView(pendingViewKey);
                  if (pendingScope) onScopeChange(pendingScope);
                  setPendingViewKey(null);
                  setPendingScope(null);
                }}
              >
                放弃并切换
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
        <AlertDialog open={deleteConfirmOpen} onOpenChange={setDeleteConfirmOpen}>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>删除“{selectedView?.name}”？</AlertDialogTitle>
              <AlertDialogDescription>
                仅删除保存的视图配置，不会删除任务或标注。此操作无法撤销。
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>取消</AlertDialogCancel>
              <AlertDialogAction
                onClick={async () => {
                  await removeCurrent();
                  setDeleteConfirmOpen(false);
                }}
              >
                确认删除
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      </DataManagerLensTabs>
    </div>
  );
}

function TaskPreview({
  task,
  width,
  height,
}: {
  task: DataManagerTask;
  width: number;
  height: number;
}) {
  if (task.file_type === "point_cloud") {
    return (
      <div
        role="img"
        aria-label="点云数据"
        className={cn(
          "flex items-center justify-center rounded-md bg-muted text-2xs text-foreground",
          width === 44 ? "h-9 w-11" : "h-32 w-40",
        )}
      >
        点云
      </div>
    );
  }
  return (
    <Thumbnail
      src={task.thumbnail_url}
      blurhash={task.blurhash}
      alt={task.file_name}
      width={width}
      height={height}
    />
  );
}

function renderCell(task: DataManagerTask, column: string) {
  switch (column) {
    case "display_id":
      return <span className="mono">{task.display_id}</span>;
    case "file_name":
      return task.file_name || "—";
    case "status":
      return (
        <Badge
          variant={
            task.status === "completed"
              ? "success"
              : task.status === "review"
                ? "warning"
                : "outline"
          }
        >
          {statusLabel(task.status)}
        </Badge>
      );
    case "annotation_count":
      return task.annotation_count.toLocaleString();
    case "pending_prediction_shape_count":
      return task.pending_prediction_shape_count ? (
        <Badge variant="warning">{task.pending_prediction_shape_count}</Badge>
      ) : (
        "0"
      );
    case "low_confidence_prediction_shape_count":
      return task.low_confidence_prediction_shape_count ? (
        <Badge variant="warning">{task.low_confidence_prediction_shape_count}</Badge>
      ) : (
        "0"
      );
    case "pending_tracker_job_count":
      return task.pending_tracker_job_count ? (
        <Badge variant="warning">{task.pending_tracker_job_count}</Badge>
      ) : (
        "0"
      );
    case "annotation_source_counts": {
      const parts = [
        ["人工", task.annotation_source_counts.manual],
        ["AI", task.annotation_source_counts.prediction_based],
        ["追踪", task.annotation_source_counts.ai_tracker],
        ["插值", task.annotation_source_counts.interpolated],
      ].filter(([, count]) => Number(count) > 0);
      return parts.length ? parts.map(([label, count]) => `${label} ${count}`).join(" · ") : "—";
    }
    case "track_count":
      return task.track_count.toLocaleString();
    case "prediction_count":
      return task.prediction_count.toLocaleString();
    case "unresolved_issue_count":
    case "unresolved_feedback_count":
      return task.unresolved_issue_count ? (
        <Badge variant="warning">{task.unresolved_issue_count}</Badge>
      ) : (
        "0"
      );
    case "comment_count":
      return task.comment_count.toLocaleString();
    case "scene_name":
      return task.scene_name ?? "—";
    case "frame_index":
      return task.frame_index ?? "—";
    case "last_activity_at":
      return formatDate(task.last_activity_at);
    case "assignee":
      return (task.effective_assignee ?? task.assignee)?.name ?? "—";
    case "reviewer":
      return (task.effective_reviewer ?? task.reviewer)?.name ?? "—";
    case "duration":
      return task.video_metadata?.duration_ms === null ||
        task.video_metadata?.duration_ms === undefined
        ? "—"
        : `${(task.video_metadata.duration_ms / 1000).toFixed(1)}s`;
    case "fps":
      return task.video_metadata?.fps === null || task.video_metadata?.fps === undefined
        ? "—"
        : task.video_metadata.fps.toFixed(2);
    case "frame_count":
      return task.video_metadata?.frame_count ?? "—";
    case "resolution":
      return task.image_width && task.image_height
        ? `${task.image_width} × ${task.image_height}`
        : "—";
    case "keyframe_count":
      return task.keyframe_count.toLocaleString();
    case "outside_range_count":
      return task.outside_range_count.toLocaleString();
    case "camera_count":
      return task.camera_count.toLocaleString();
    case "calibration_issue_count":
      return task.calibration_issue_count ? (
        <Badge variant="warning">{task.calibration_issue_count}</Badge>
      ) : (
        "0"
      );
    case "scene_total_frames":
      return task.scene_total_frames ?? "—";
    default:
      return "—";
  }
}
