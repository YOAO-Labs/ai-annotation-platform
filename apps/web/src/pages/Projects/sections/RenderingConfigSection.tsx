import { useState } from "react";
import { Card } from "@/components/ui/Card";
import { useToastStore } from "@/components/ui/Toast";
import { useUpdateProject } from "@/hooks/useProjects";
import type { ProjectResponse, ProjectRenderingConfig } from "@/api/projects";
import { RenderingConfigEditor } from "./RenderingConfigEditor";

export function RenderingConfigSection({ project }: { project: ProjectResponse }) {
  const pushToast = useToastStore((s) => s.push);
  const update = useUpdateProject(project.id);
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
    </Card>
  );
}
