import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import type { ProjectResponse } from "@/api/projects";
import styles from "../CreateProjectWizard.module.css";

export function Step7Success({
  project,
  summary,
  onOpenProject,
  onOpenSettings,
  onOpenReadiness,
  onDone,
}: {
  project: ProjectResponse;
  summary: { datasets: number; members: number };
  onOpenProject: () => void;
  onOpenSettings: () => void;
  onOpenReadiness: () => void;
  onDone: () => void;
}) {
  const canOpen = project.type_key === "image-det" || project.type_key === "video-track";
  return (
    <div className={styles.successRoot}>
      <div className={styles.successIcon}>
        <Icon name="check" size={28} />
      </div>
      <div className={styles.successTitle}>{project.name}</div>
      <div className={styles.successMeta}>
        <span className="mono">{project.display_id}</span> · {project.type_label}
      </div>
      <div className={styles.successSummary}>
        已关联 {summary.datasets} 个数据集 · 已添加 {summary.members} 位成员
        <div className={styles.successWarning}>项目已创建；创建项目不会自动分派任务。</div>
        {summary.datasets === 0 && (
          <div className={styles.successWarning}>尚未关联数据集，可去设置页继续配置</div>
        )}
      </div>
      <div className={styles.successActions}>
        <Button onClick={onOpenReadiness}>
          <Icon name="check" size={12} />
          开工准备
        </Button>
        <Button variant="primary" onClick={onOpenSettings}>
          <Icon name="settings" size={12} />
          项目设置
        </Button>
        {canOpen && (
          <Button onClick={onOpenProject}>
            <Icon name="target" size={12} />
            打开工作台
          </Button>
        )}
        <Button variant="ghost" onClick={onDone}>
          完成
        </Button>
      </div>
    </div>
  );
}
