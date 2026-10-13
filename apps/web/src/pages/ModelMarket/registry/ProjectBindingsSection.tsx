/**
 * v0.23.4 P3 · registry "项目绑定" tab (Super Admin only).
 *
 * Plan §4.3（模型市场多 TAB UI 优化 · 阶段一）: default by-project view
 * (项目 / 关联服务池 / 可用实例 / 风险 / 项目设置) with a by-pool reverse
 * lookup toggle. The previously inferred 「主服务池」 column is gone — it was
 * derived from routable counts, not real configuration; the authoritative
 * binding stays in project settings. Every view carries the annotation
 * 「依据已启用实例关联」 so the derivation is not mistaken for configured fact.
 *
 * URL-owned state (plan §5): `project_q` search (project names in by-project
 * view, pool names in by-pool view), `binding_view`, and the removable
 * `binding_pool` scope condition.
 *
 * Alert (warning badge) when an AI-enabled project has 0 routable instances in
 * its pool. Read-only — "修改" links to /projects/:id/settings.
 */
import { useMemo, type ReactNode } from "react";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/shadcn/ui/table";
import { Input } from "@/components/shadcn/ui/input";
import { ActiveFilterChip } from "@/components/filters/ActiveFilterChip";

import { EmptyState, LoadingState, NullCell, ShortCopyableId, UrlIssueChips } from "./registryUi";
import type { RegistrySectionProps } from "./registryTypes";
import type { PoolViewModel } from "../runtimeTopology";
import { urlIssuesForView } from "../marketUrlState";

interface ProjectBinding {
  project_id: string;
  project_name: string;
  pools: PoolViewModel[];
  routableCount: number;
  hasEnabledBackend: boolean;
}

interface PoolBinding {
  pool: PoolViewModel;
  projects: Array<{ project_id: string; project_name: string }>;
}

export function ProjectBindingsSection({
  scope,
  url,
  patchUrl,
  urlIssues,
}: RegistrySectionProps): ReactNode {
  const { overview, vm, loading } = scope;

  const projectBindings = useMemo<ProjectBinding[]>(() => {
    if (!overview) return [];
    return overview.projects.map((p) => {
      // Find pools that contain at least one of this project's enabled backends.
      const backendIds = new Set(p.backends.map((b) => b.id));
      const pools = vm.pools.filter((pool) =>
        pool.members.some((m) => backendIds.has(m.registry_id)),
      );
      const routableCount = pools.reduce((sum, pool) => sum + pool.availability.routable, 0);
      return {
        project_id: p.project_id,
        project_name: p.project_name,
        pools,
        routableCount,
        hasEnabledBackend: p.backends.length > 0,
      };
    });
  }, [overview, vm.pools]);

  const poolBindings = useMemo<PoolBinding[]>(() => {
    if (!overview) return [];
    return vm.pools.map((pool) => {
      const memberIds = new Set(pool.members.map((m) => m.registry_id));
      const projects: Array<{ project_id: string; project_name: string }> = [];
      for (const proj of overview.projects) {
        if (proj.backends.some((b) => memberIds.has(b.id))) {
          projects.push({ project_id: proj.project_id, project_name: proj.project_name });
        }
      }
      return { pool, projects };
    });
  }, [overview, vm.pools]);

  const isByProject = url.bindingView === "by-project";
  const focusPool = url.bindingPool
    ? (vm.pools.find((p) => p.id === url.bindingPool) ?? null)
    : null;

  const filteredProjectBindings = useMemo(() => {
    let rows = projectBindings;
    if (url.bindingPool) {
      rows = rows.filter((b) => b.pools.some((p) => p.id === url.bindingPool));
    }
    if (url.projectQ) {
      const q = url.projectQ.trim().toLowerCase();
      rows = rows.filter(
        (b) => b.project_name.toLowerCase().includes(q) || b.project_id.toLowerCase().includes(q),
      );
    }
    return rows;
  }, [projectBindings, url.bindingPool, url.projectQ]);

  const filteredPoolBindings = useMemo(() => {
    let rows = poolBindings;
    if (url.bindingPool) {
      rows = rows.filter((b) => b.pool.id === url.bindingPool);
    }
    if (url.projectQ) {
      const q = url.projectQ.trim().toLowerCase();
      rows = rows.filter(
        (b) => b.pool.name.toLowerCase().includes(q) || b.pool.id.toLowerCase().includes(q),
      );
    }
    return rows;
  }, [poolBindings, url.bindingPool, url.projectQ]);

  if (loading.overview) {
    return <LoadingState label="加载项目绑定…" />;
  }

  if (!overview) {
    return <EmptyState icon="folder" message="项目绑定概览不可用" hint="仅超级管理员可见。" />;
  }

  if (overview.projects.length === 0 && vm.pools.length === 0) {
    return (
      <div className="flex flex-col gap-3">
        <BindingToolbar
          url={url}
          patchUrl={patchUrl}
          urlIssues={urlIssues}
          isByProject={isByProject}
          focusPoolName={focusPool?.name ?? null}
        />
        <EmptyState
          icon="folder"
          message="尚无项目启用 AI 或服务池"
          hint="在项目设置里启用 AI 并绑定 backend 后会出现在这里。"
        />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <BindingToolbar
        url={url}
        patchUrl={patchUrl}
        urlIssues={urlIssues}
        isByProject={isByProject}
        focusPoolName={focusPool?.name ?? null}
      />

      {isByProject ? (
        <Table containerClassName="overflow-hidden rounded-lg border border-border bg-card">
          <TableHeader>
            <TableRow>
              <TableHead>项目</TableHead>
              <TableHead>关联服务池</TableHead>
              <TableHead>可用实例</TableHead>
              <TableHead>风险</TableHead>
              <TableHead className="text-right">操作</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredProjectBindings.map((b) => (
              <ProjectBindingRow key={b.project_id} binding={b} />
            ))}
            {filteredProjectBindings.length === 0 && (
              <TableRow>
                <TableCell colSpan={5}>
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    没有匹配的项目
                  </div>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      ) : (
        <Table containerClassName="overflow-hidden rounded-lg border border-border bg-card">
          <TableHeader>
            <TableRow>
              <TableHead>服务池</TableHead>
              <TableHead>可路由实例</TableHead>
              <TableHead>绑定项目</TableHead>
              <TableHead>状态</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {filteredPoolBindings.map((b) => (
              <PoolBindingRow key={b.pool.id} binding={b} />
            ))}
            {filteredPoolBindings.length === 0 && (
              <TableRow>
                <TableCell colSpan={4}>
                  <div className="p-6 text-center text-sm text-muted-foreground">
                    没有匹配的服务池
                  </div>
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      )}
    </div>
  );
}

function BindingToolbar({
  url,
  patchUrl,
  urlIssues,
  isByProject,
  focusPoolName,
}: {
  url: RegistrySectionProps["url"];
  patchUrl: RegistrySectionProps["patchUrl"];
  urlIssues: RegistrySectionProps["urlIssues"];
  isByProject: boolean;
  focusPoolName: string | null;
}): ReactNode {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative">
          <Icon
            name="search"
            size={12}
            className="pointer-events-none absolute top-1/2 left-2 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            aria-label={isByProject ? "搜索项目" : "搜索服务池"}
            placeholder={isByProject ? "搜索项目名称 / ID" : "搜索服务池名称 / ID"}
            value={url.projectQ}
            onChange={(e) => patchUrl({ projectQ: e.target.value })}
            className="h-8 w-56 pl-7 text-xs"
          />
        </div>
        <span className="text-2xs text-muted-foreground">视图</span>
        <Button
          size="sm"
          variant={isByProject ? "primary" : "ghost"}
          onClick={() => patchUrl({ bindingView: "by-project" })}
          aria-pressed={isByProject}
        >
          按项目
        </Button>
        <Button
          size="sm"
          variant={!isByProject ? "primary" : "ghost"}
          onClick={() => patchUrl({ bindingView: "by-pool" })}
          aria-pressed={!isByProject}
        >
          按服务池
        </Button>
        <span
          className="ml-auto inline-flex items-center gap-1 text-2xs text-muted-foreground"
          title="项目与服务池的关联按当前拓扑和已启用后端展示推导，不代表已配置事实；真实配置在项目设置里。"
        >
          <Icon name="info" size={11} />
          依据已启用实例关联
        </span>
      </div>
      {url.bindingPool && (
        <div className="flex flex-wrap items-center gap-2">
          <ActiveFilterChip
            label={focusPoolName ? `仅显示服务池「${focusPoolName}」的关联` : "按服务池 ID 限定"}
            value={focusPoolName ? undefined : url.bindingPool}
            onRemove={() => patchUrl({ bindingPool: "" })}
          />
        </div>
      )}
      <UrlIssueChips
        issues={urlIssuesForView(urlIssues, "projects")}
        onDismiss={(key) => {
          if (key === "binding_view") patchUrl({ bindingView: "by-project" });
        }}
      />
    </div>
  );
}

function ProjectBindingRow({ binding }: { binding: ProjectBinding }): ReactNode {
  const hasRisk =
    binding.hasEnabledBackend && binding.routableCount === 0 && binding.pools.length > 0;
  const noPoolButEnabled = binding.hasEnabledBackend && binding.pools.length === 0;
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span className="font-medium">{binding.project_name}</span>
          <ShortCopyableId value={binding.project_id} label="项目 ID" />
        </div>
      </TableCell>
      <TableCell>
        {binding.pools.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {binding.pools.map((p) => (
              <Badge key={p.id} variant="outline" className="text-2xs">
                {p.name}
              </Badge>
            ))}
          </div>
        ) : (
          <NullCell>未绑定服务池</NullCell>
        )}
      </TableCell>
      <TableCell>
        <span className="text-sm">{binding.routableCount}</span>
      </TableCell>
      <TableCell>
        {hasRisk ? (
          <Badge variant="danger">
            <Icon name="alert-triangle" size={11} />
            <span>池内无可路由实例</span>
          </Badge>
        ) : noPoolButEnabled ? (
          <Badge variant="warning">
            <Icon name="alert-triangle" size={11} />
            <span>已启用 backend 但未纳管到服务池</span>
          </Badge>
        ) : (
          <NullCell>—</NullCell>
        )}
      </TableCell>
      <TableCell className="text-right">
        <Link
          to={`/projects/${binding.project_id}/settings?section=ml-backends`}
          className="whitespace-nowrap text-xs text-brand no-underline hover:underline"
        >
          打开项目设置 →
        </Link>
      </TableCell>
    </TableRow>
  );
}

function PoolBindingRow({ binding }: { binding: PoolBinding }): ReactNode {
  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col gap-0.5">
          <span className="font-medium">{binding.pool.name}</span>
          <ShortCopyableId value={binding.pool.id} label="服务池 ID" />
        </div>
      </TableCell>
      <TableCell>
        <span className="text-sm">
          <span className="text-status-positive">{binding.pool.availability.routable}</span>
          <span className="text-muted-foreground"> / {binding.pool.availability.total}</span>
        </span>
      </TableCell>
      <TableCell>
        {binding.projects.length > 0 ? (
          <div className="flex flex-wrap gap-1">
            {binding.projects.map((p) => (
              <Badge key={p.project_id} variant="outline" className="text-2xs">
                {p.project_name}
              </Badge>
            ))}
          </div>
        ) : (
          <NullCell>无项目绑定</NullCell>
        )}
      </TableCell>
      <TableCell>
        <Badge
          variant={
            binding.pool.status === "offline"
              ? "danger"
              : binding.pool.status === "degraded"
                ? "warning"
                : binding.pool.status === "healthy"
                  ? "success"
                  : "outline"
          }
        >
          {binding.pool.status}
        </Badge>
      </TableCell>
    </TableRow>
  );
}
