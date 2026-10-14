// v0.10.10 · I17.3 · 项目级渲染配置覆盖（ProjectSettings 子页）。
// v0.10.18 · 视图层抽出为 RenderingConfigEditor, Section 仅作保存外壳.
//
// 字段集与 User.preferences.workbench 同：smoothImage / cssImageFilter /
// controlPointsSize / snapToGrid（不含 longTaskSampleRate）。
// PATCH 整个 rendering_config 对象；后端 Pydantic ProjectRenderingConfig
// (extra=forbid, 字段范围校验) 兜底。

import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { useToastStore } from "@/components/ui/Toast";
import { useUpdateProject } from "@/hooks/useProjects";
import { useProjectAccess } from "@/hooks/useProjectAccess";
import { Switch } from "@/components/ui/Switch";
import {
  DEFAULT_POINT_CLOUD_ENHANCEMENT_CONFIG,
  type PointCloudEnhancementConfig,
  type ProjectResponse,
  type ProjectRenderingConfig,
} from "@/api/projects";
import { RenderingConfigEditor } from "./RenderingConfigEditor";
import { PointCloudPreparation } from "./PointCloudPreparation";

export function RenderingConfigSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const update = useUpdateProject(project.id);
  const access = useProjectAccess(project.id);
  const canManage = access.hasCapability("project.manage");
  const enhancement =
    project.point_cloud_enhancement_config ?? DEFAULT_POINT_CLOUD_ENHANCEMENT_CONFIG;
  const onEnhancementChange = (config: PointCloudEnhancementConfig) => {
    if (!canManage || update.isPending) return;
    update.mutate(
      { point_cloud_enhancement_config: config },
      {
        onError: () => pushToast({ msg: "AI 补强实验设置保存失败", kind: "warning" }),
      },
    );
  };
  const [draft, setDraft] = useState<ProjectRenderingConfig>(project.rendering_config ?? {});

  const onChange = (next: ProjectRenderingConfig) => {
    setDraft(next);
    update.mutate(
      { rendering_config: next },
      {
        onError: () => pushToast({ msg: "保存失败", kind: "warning" }),
      },
    );
  };

  return (
    <Card>
      <div className="border-b border-border px-4 py-3.5">
        <h3 className="text-sm font-semibold">工作台规范（项目级覆盖）</h3>
        <p className="m-0 mt-1 text-xs text-muted-foreground">
          项目级覆盖优先于成员的个人「标注偏好」，也可锁定 3D 新框尺寸、关键帧复制策略和 AI
          追踪默认模型。
        </p>
      </div>
      <div className="p-4">
        <RenderingConfigEditor value={draft} onChange={onChange} disabled={update.isPending} />
        {update.isPending && <div className="mt-3 text-xs text-muted-foreground">保存中…</div>}
      </div>
      <section className="border-t border-border p-4" aria-label="AI 点云补强实验">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h4 className="m-0 text-sm font-semibold">AI 当前帧点云补强（实验）</h4>
            <p className="m-0 mt-1 text-xs text-muted-foreground">
              VGGT-Ω 生成辅助形状；当前真实 LiDAR 始终用于标注。成员需自行选择 AI
              补强，查看不会启动推理。
            </p>
          </div>
          <Switch
            checked={enhancement.enabled}
            disabled={!canManage || update.isPending}
            label="允许成员使用实验"
            data-testid="pointcloud-enhancement-project-toggle"
            onChange={(enabled) => onEnhancementChange({ ...enhancement, enabled })}
          />
        </div>
        <PointCloudPreparation
          projectId={project.id}
          config={enhancement}
          canManage={canManage}
          saving={update.isPending}
          onConfigChange={onEnhancementChange}
        />
      </section>
    </Card>
  );
}
