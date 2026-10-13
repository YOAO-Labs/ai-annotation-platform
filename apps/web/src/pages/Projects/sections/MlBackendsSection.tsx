import { useEffect, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Icon } from "@/components/ui/Icon";
import { Modal } from "@/components/ui/Modal";
import { Switch } from "@/components/ui/Switch";
import { useToastStore } from "@/components/ui/Toast";
import {
  useAvailableMLBackends,
  useSetMLBackendEnablement,
  useMLBackendHealth,
} from "@/hooks/useMLBackends";
import { useUpdateProject } from "@/hooks/useProjects";
import { usePermissions } from "@/hooks/usePermissions";
import {
  mlBackendsApi,
  mlBackendSetupQueryKey,
  type MLBackendCapability,
  type ProjectMLBackendItem,
} from "@/api/ml-backends";
import type { ProjectResponse } from "@/api/projects";
import type { MLBackendResponse } from "@/types";
import { LABEL_CLASS } from "./formClasses";

function cn(...xs: Array<string | false | null | undefined>): string {
  return xs.filter(Boolean).join(" ");
}

const CONTROL_CLASS =
  "box-border w-full appearance-none rounded-md border border-border bg-muted px-2.5 py-2 text-sm text-foreground outline-none [font-family:inherit]";
const TABLE_HEAD_CELL =
  "whitespace-nowrap border-b border-border bg-muted px-3 py-1.5 text-left text-xs font-medium text-muted-foreground";
const TABLE_CELL = "border-b border-border px-3 py-2 align-middle";

const STATE_VARIANT: Record<string, "success" | "warning" | "outline" | "danger"> = {
  connected: "success",
  disconnected: "outline",
  error: "danger",
};

// v0.19.0 · ADR-0044 · 「管理 backend」悬浮面板: 列出全部全局 backend, 在此勾选启用/停用。
// 主表只展示已启用项, 增删启用都在本面板里做。
function ManageBackendsPanel({
  open,
  onClose,
  items,
  canManage,
  pending,
  onToggle,
}: {
  open: boolean;
  onClose: () => void;
  items: ProjectMLBackendItem[];
  canManage: boolean;
  pending: boolean;
  onToggle: (item: ProjectMLBackendItem, next: boolean) => void;
}) {
  return (
    <Modal open={open} onClose={onClose} title="管理项目 ML backend" width={720}>
      <div className="mb-3 text-xs text-muted-foreground">
        勾选启用本项目要用的全局 backend，并可按项目调整阈值覆盖；全局 backend
        由超管在「模型市场」注册。
      </div>
      {items.length === 0 ? (
        <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
          <Icon name="bot" size={28} className="mb-1.5 opacity-25" />
          <div>暂无可用的全局 ML backend</div>
          <div className="mt-1 text-xs">
            请由超管在「模型市场」注册全局 backend 后，在此勾选启用
          </div>
        </div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.map((item) => {
            const b = item.backend;
            return (
              <li
                key={b.id}
                className="flex items-start gap-3 rounded-md border border-border bg-background p-3"
              >
                <input
                  type="checkbox"
                  aria-label={`启用 ${b.name}`}
                  checked={item.enabled}
                  disabled={!canManage || pending}
                  onChange={(e) => onToggle(item, e.target.checked)}
                  className="mt-1 accent-violet-500"
                />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="truncate text-sm font-medium" title={b.name}>
                      {b.name}
                    </span>
                    <Badge variant={b.is_interactive ? "ai" : "outline"}>
                      {b.is_interactive ? "交互式" : "批量"}
                    </Badge>
                    <Badge variant={STATE_VARIANT[b.state] ?? "outline"} dot>
                      {b.state}
                    </Badge>
                  </div>
                  <div className="mono mt-0.5 truncate text-xs text-muted-foreground" title={b.url}>
                    {b.url}
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Modal>
  );
}

export function MlBackendsSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const { role } = usePermissions();
  const canManage = role === "super_admin" || role === "project_admin";

  const [manageOpen, setManageOpen] = useState(false);

  const { data, isLoading, isError, error } = useAvailableMLBackends(project.id);
  const items = data?.items ?? [];
  const enabledItems = items.filter((it) => it.enabled);
  const enabledBackends = enabledItems.map((it) => it.backend);

  const setEnablement = useSetMLBackendEnablement(project.id);
  const health = useMLBackendHealth(project.id);
  // 行内「设为主后端」快捷设置项目主后端，免回基本信息 tab 手选。
  const updateProject = useUpdateProject(project.id);
  // v0.19.0 · ai_enabled 不再手动开关, 自动派生「设了项目主后端即视为启用 AI」。
  const [mlBackendId, setMlBackendId] = useState<string | null>(project.ml_backend_id ?? null);
  const [iouThreshold, setIouThreshold] = useState(project.iou_dedup_threshold ?? 0.7);

  // 改动即时生效:下拉 / 行内「设为主后端」/ IoU 滑块都各自直接落库,不再有「保存」批量提交。
  // 本地态只作乐观显示 + 跟随服务端回灌(切项目、外部改动、提交失败回滚都靠它)。
  useEffect(() => {
    setMlBackendId(project.ml_backend_id ?? null);
    setIouThreshold(project.iou_dedup_threshold ?? 0.7);
  }, [project.id, project.ml_backend_id, project.iou_dedup_threshold]);

  // 设/清项目主后端 → 即时落库(ai_enabled 跟随派生)。乐观更新本地下拉,失败回滚。
  const commitBackend = (id: string | null, name?: string) => {
    setMlBackendId(id);
    updateProject.mutate(
      { ai_enabled: id != null, ml_backend_id: id },
      {
        onSuccess: () =>
          pushToast({
            msg: id ? `已设为项目主后端「${name ?? ""}」` : "已清除项目主后端（AI 预标注停用）",
            kind: "success",
          }),
        onError: (e) => {
          setMlBackendId(project.ml_backend_id ?? null);
          pushToast({ msg: "设置失败", sub: (e as Error).message });
        },
      },
    );
  };

  // 交互式 AI 工具总开关 (工作台 smart-point / smart-box / exemplar / magic-box)。
  // 关闭后这些工具在工作台整组隐藏; 具体某个工具是否可用仍取决于后端 supported_prompts
  // (不支持则置灰), 以及其产出几何所属工具单位 (region / bbox) 是否启用。
  const commitAiInteractive = (next: boolean) => {
    updateProject.mutate(
      { ai_interactive_enabled: next },
      {
        onSuccess: () =>
          pushToast({
            msg: next ? "已启用交互式 AI 工具" : "已停用交互式 AI 工具",
            kind: "success",
          }),
        onError: (e) => pushToast({ msg: "保存失败", sub: (e as Error).message }),
      },
    );
  };

  // IoU 阈值滑块松手/失焦时提交一次(拖动中只更新本地,不逐帧发请求);无变化跳过。
  const commitIou = (value: number) => {
    if (Math.abs(value - (project.iou_dedup_threshold ?? 0.7)) < 0.001) return;
    updateProject.mutate(
      { iou_dedup_threshold: value },
      {
        onSuccess: () => pushToast({ msg: `去重阈值已保存 ${value.toFixed(2)}`, kind: "success" }),
        onError: (e) => {
          setIouThreshold(project.iou_dedup_threshold ?? 0.7);
          pushToast({ msg: "保存失败", sub: (e as Error).message });
        },
      },
    );
  };

  const onBind = (b: MLBackendResponse) => commitBackend(b.id, b.name);

  // v0.19.0 · ADR-0044 · 启用/停用某全局 backend。
  const onToggleEnabled = (item: ProjectMLBackendItem, next: boolean) => {
    setEnablement.mutate(
      { registryId: item.backend.id, payload: { enabled: next } },
      {
        onSuccess: () =>
          pushToast({
            msg: next ? `已启用「${item.backend.name}」` : `已停用「${item.backend.name}」`,
            kind: "success",
          }),
        onError: (e) => pushToast({ msg: "操作失败", sub: (e as Error).message }),
      },
    );
  };

  // 仅对已启用 backend 拉 /setup 拿 supported_prompts; 失败容忍, 列显示 "—"。
  // 管理面板低频, 不做合并端点; 未来 N>5 再优化。
  const capabilities = useQueries({
    queries: enabledBackends.map((b) => ({
      queryKey: mlBackendSetupQueryKey(project.id, b.id),
      queryFn: () => mlBackendsApi.setup(project.id, b.id),
      staleTime: 60_000,
      retry: false,
    })),
  });
  const capById = new Map(enabledBackends.map((b, i) => [b.id, capabilities[i]]));

  const onHealth = (b: MLBackendResponse) => {
    health.mutate(b.id, {
      onSuccess: (res) =>
        pushToast({
          msg: `${b.name}: ${res.status}`,
          kind: res.status === "connected" ? "success" : "warning",
        }),
      onError: (e) => pushToast({ msg: "健康检查失败", sub: (e as Error).message }),
    });
  };

  return (
    <Card>
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3.5">
        <div>
          <h3 className="text-sm font-semibold">
            ML 模型
            <span
              data-testid="ml-backend-quota"
              className="ml-2 text-xs font-medium text-muted-foreground"
            >
              已启用 {enabledBackends.length} / {items.length}
            </span>
          </h3>
          <div className="mt-1 text-xs text-muted-foreground">
            本表仅显示本项目已启用的 ML backend；点「管理 backend」可启用/停用全局
            backend。推理参数在工作台 / 预标运行时按 backend 自报的 /setup 调。
          </div>
        </div>
        {canManage && (
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setManageOpen(true)}
            disabled={isLoading || isError}
          >
            <Icon name="plus" />
            管理 backend
          </Button>
        )}
      </div>

      <div className="border-b border-border bg-background px-4 py-3.5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <span className="inline-flex items-center gap-2 text-sm font-semibold">
            <Icon name="sparkles" size={14} className="text-status-info" />
            AI 预标注设置
          </span>
          <span className="text-xs text-muted-foreground">改动即时生效</span>
        </div>

        <div className="grid grid-cols-[minmax(260px,1.2fr)_minmax(220px,1fr)] gap-3.5">
          <div>
            <label className={LABEL_CLASS}>项目主后端</label>
            <select
              value={mlBackendId ?? ""}
              disabled={!canManage || updateProject.isPending}
              onChange={(e) => {
                const id = e.target.value || null;
                commitBackend(id, enabledBackends.find((b) => b.id === id)?.name);
              }}
              className={cn(CONTROL_CLASS, "cursor-pointer")}
            >
              <option value="">未设项目主后端（项目按肉眼标注运行，AI 待接入）</option>
              {enabledBackends.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                  {b.state === "connected" ? " · 在线" : ` · ${b.state}`}
                  {b.is_interactive ? " · 交互式" : ""}
                </option>
              ))}
            </select>
            <div className="mt-1 text-xs leading-normal text-muted-foreground">
              设了项目主后端即视为启用 AI 预标注（留空 = 不启用）。主后端用于工作台 AI
              与新建预标配置的初始选择 / fallback；多阶段预标注中，每个阶段显式选择的 backend/model
              仍然独立生效。平台所有“模型名”展示均直接来自 backend.name。
              {enabledBackends.length === 0 && (
                <span className="ml-1 text-status-caution">
                  暂无已启用 backend；请点右上「管理 backend」勾选启用。
                </span>
              )}
            </div>
          </div>

          <div>
            <label className={LABEL_CLASS}>
              AI 框去重阈值{" "}
              <span className="font-normal text-muted-foreground">
                同类 AI 框与人工框 IoU 高于此值时淡化
              </span>
            </label>
            <div className="flex min-h-9 items-center gap-3">
              <input
                type="range"
                min={0.3}
                max={0.95}
                step={0.05}
                value={iouThreshold}
                disabled={!canManage}
                onChange={(e) => setIouThreshold(Number(e.target.value))}
                onPointerUp={(e) => commitIou(Number((e.currentTarget as HTMLInputElement).value))}
                onKeyUp={(e) => commitIou(Number((e.currentTarget as HTMLInputElement).value))}
                onBlur={(e) => commitIou(Number(e.currentTarget.value))}
                className="flex-1 accent-violet-500"
              />
              <span className={cn("mono", "min-w-[48px] text-right text-sm text-foreground")}>
                {iouThreshold.toFixed(2)}
              </span>
            </div>
          </div>

          <div>
            <label className={LABEL_CLASS}>
              交互式 AI 工具{" "}
              <span className="font-normal text-muted-foreground">
                工作台的 SAM 点 / 框 / 示例框 与 Magic Box
              </span>
            </label>
            <div className="flex min-h-9 items-center">
              <Switch
                checked={project.ai_interactive_enabled ?? true}
                onChange={commitAiInteractive}
                disabled={!canManage || updateProject.isPending}
                label={
                  (project.ai_interactive_enabled ?? true)
                    ? "标注员可在工作台使用交互式 AI 工具"
                    : "已停用：工作台不显示交互式 AI 工具"
                }
                data-testid="ai-interactive-toggle"
              />
            </div>
            <p className="mt-1 text-xs text-muted-foreground">
              具体工具还需后端支持对应交互模式（不支持则置灰），且其产出几何所属的工具单位
              （多边形归「区域」、矩形框归「矩形框」）已在「类别与属性」中启用。
            </p>
          </div>
        </div>
      </div>

      <div className="p-3">
        {isLoading && <div className="p-6 text-center text-sm text-muted-foreground">加载中…</div>}
        {isError && (
          <div className="p-6 text-center text-sm text-status-danger">
            <Icon name="warning" size={14} className="mr-1.5" />
            加载失败：{(error as Error)?.message ?? "未知错误"}
          </div>
        )}
        {!isLoading && !isError && enabledItems.length === 0 && (
          <div className="rounded-md border border-dashed border-border p-8 text-center text-sm text-muted-foreground">
            <Icon name="bot" size={28} className="mb-1.5 opacity-25" />
            <div>本项目暂未启用任何 ML backend</div>
            <div className="mt-1 text-xs">
              {items.length > 0
                ? "点右上「管理 backend」从全局注册表勾选启用"
                : "请由超管在「模型市场」注册全局 backend 后，再来此启用"}
            </div>
          </div>
        )}
        {!isLoading && !isError && enabledItems.length > 0 && (
          <div className="w-full overflow-x-auto">
            <table className="w-full min-w-[920px] border-separate border-spacing-0 text-sm">
              <thead>
                <tr>
                  {["名称", "URL", "类型", "能力", "状态", "操作"].map((h) => (
                    <th key={h} className={TABLE_HEAD_CELL}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {enabledItems.map((item) => {
                  const b = item.backend;
                  const capQ = capById.get(b.id);
                  const cap = capQ?.data as MLBackendCapability | undefined;
                  return (
                    <tr key={b.id}>
                      <td className={TABLE_CELL}>
                        <div className="max-w-[180px] truncate" title={b.name}>
                          {b.name}
                        </div>
                      </td>
                      <td
                        className={cn(
                          TABLE_CELL,
                          "mono max-w-[280px] truncate text-xs text-muted-foreground",
                        )}
                        title={b.url}
                      >
                        {b.url}
                      </td>
                      <td className={cn(TABLE_CELL, "whitespace-nowrap")}>
                        <Badge variant={b.is_interactive ? "ai" : "outline"}>
                          {b.is_interactive ? "交互式" : "批量"}
                        </Badge>
                      </td>
                      <td className={TABLE_CELL}>
                        {capQ?.isLoading && (
                          <span className="text-xs text-muted-foreground">…</span>
                        )}
                        {capQ?.isError && <span className="text-xs text-muted-foreground">—</span>}
                        {cap?.supported_prompts && (
                          <div className="inline-flex flex-wrap gap-1">
                            {cap.supported_prompts.map((p) => (
                              <Badge key={p} variant="outline">
                                {p}
                              </Badge>
                            ))}
                            {/* v0.10.37 · 视频追踪能力 (supported_trackers 非空 ⇒ 支持 video 模态) */}
                            {cap.supported_trackers?.map((t) => (
                              <Badge key={t} variant="ai">
                                <Icon name="video" size={10} />
                                {t}
                              </Badge>
                            ))}
                          </div>
                        )}
                      </td>
                      <td className={cn(TABLE_CELL, "whitespace-nowrap")}>
                        <Badge variant={STATE_VARIANT[b.state] ?? "outline"} dot>
                          {b.state}
                        </Badge>
                      </td>
                      <td className={TABLE_CELL}>
                        <div className="inline-flex items-center gap-1.5 whitespace-nowrap">
                          {project.ml_backend_id !== b.id && (
                            <Button
                              size="xs"
                              variant="ai"
                              onClick={() => onBind(b)}
                              disabled={!canManage || updateProject.isPending}
                              title={
                                canManage
                                  ? "设为本项目主后端（同时启用 AI）"
                                  : "需要 PROJECT_ADMIN 权限"
                              }
                            >
                              设为主后端
                            </Button>
                          )}
                          {project.ml_backend_id === b.id && <Badge variant="ai">主后端</Badge>}
                          <Button
                            size="xs"
                            onClick={() => onHealth(b)}
                            disabled={health.isPending}
                            title="健康检查"
                          >
                            <Icon name="refresh" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <ManageBackendsPanel
        open={manageOpen}
        onClose={() => setManageOpen(false)}
        items={items}
        canManage={canManage}
        pending={setEnablement.isPending}
        onToggle={onToggleEnabled}
      />
    </Card>
  );
}
