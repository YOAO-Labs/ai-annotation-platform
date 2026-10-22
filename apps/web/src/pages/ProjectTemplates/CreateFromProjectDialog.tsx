// v0.10.14 · E2 · 从已有项目导出模板对话框. 列出当前用户可见的项目, 选定后
// 调 POST /project-templates with source_project_id; 后端自动 dump
// _CLONEABLE_PROJECT_FIELDS + annotation_guide.

import { useState } from "react";

import { Button } from "@/components/ui/Button";
import { Modal } from "@/components/ui/Modal";
import { useToastStore } from "@/components/ui/Toast";
import { useProjects } from "@/hooks/useProjects";
import { useCreateProjectTemplate } from "@/hooks/useProjectTemplates";

import styles from "./ProjectTemplatesPage.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
}

export function CreateFromProjectDialog({ open, onClose }: Props) {
  const pushToast = useToastStore((s) => s.push);
  const projects = useProjects();
  const create = useCreateProjectTemplate();

  const [projectId, setProjectId] = useState<string>("");
  const [name, setName] = useState("");

  const handleSubmit = () => {
    if (!projectId) {
      pushToast({ msg: "请选择源项目", kind: "warning" });
      return;
    }
    const source = projects.data?.find((p) => p.id === projectId);
    if (!source) {
      pushToast({ msg: "源项目不存在", kind: "warning" });
      return;
    }
    create.mutate(
      {
        name: name.trim() || `${source.name} 模板`,
        type_label: source.type_label,
        type_key: source.type_key,
        source_project_id: projectId,
        scope: "private",
      },
      {
        onSuccess: () => {
          pushToast({ msg: "已从项目导出模板", kind: "success" });
          setProjectId("");
          setName("");
          onClose();
        },
        onError: (err) =>
          pushToast({
            msg: err instanceof Error ? err.message : "导出失败",
            kind: "warning",
          }),
      },
    );
  };

  return (
    <Modal open={open} onClose={onClose} title="从已有项目导出模板" width={540}>
      <div className={styles.modalBody}>
        <p className={styles.titleHint}>
          从源项目自动复制 classes / classes_config / attribute_schema / AI 配置 / 标注指引
          等可克隆字段。导出后可在模板库中编辑。
        </p>

        <label className={styles.label}>
          源项目
          <select
            className={styles.select}
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
          >
            <option value="" disabled hidden>
              请选择项目
            </option>
            {projectId && !projects.data?.some((project) => project.id === projectId) && (
              <option value={projectId} disabled hidden>
                已选项目（暂不可用）
              </option>
            )}
            {(projects.data ?? []).map((p) => (
              <option key={p.id} value={p.id}>
                {p.display_id} · {p.name}
              </option>
            ))}
          </select>
        </label>

        <label className={styles.label}>
          模板名称（可选，默认 "源项目名 模板"）
          <input
            className={styles.input}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="留空使用默认名称"
          />
        </label>

        <div className={styles.modalActions}>
          <Button variant="ghost" onClick={onClose} disabled={create.isPending}>
            取消
          </Button>
          <Button variant="primary" onClick={handleSubmit} disabled={create.isPending}>
            {create.isPending ? "导出中…" : "导出为模板"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
