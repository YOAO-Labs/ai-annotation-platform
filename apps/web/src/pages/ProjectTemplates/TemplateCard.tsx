// 模板列表卡片. 显示 name / type / classes 数 / usage_count /
// scope chip + 操作按钮 (应用 / 复制 / 编辑 / 删除).

import { Button } from "@/components/ui/Button";
import { Card } from "@/components/ui/Card";
import { Icon } from "@/components/ui/Icon";
import { PROJECT_TYPES } from "@/constants/projectTypes";
import type { ProjectTemplateOut } from "@/api/projectTemplates";

import styles from "./ProjectTemplatesPage.module.css";

interface Props {
  template: ProjectTemplateOut;
  canEdit: boolean;
  onApply: () => void;
  onDuplicate: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

const SCOPE_LABEL: Record<ProjectTemplateOut["scope"], string> = {
  private: "私有",
  organization: "组织",
  public: "公共",
};

export function TemplateCard({ template, canEdit, onApply, onDuplicate, onEdit, onDelete }: Props) {
  const type = PROJECT_TYPES.find((t) => t.key === template.type_key);
  return (
    <Card>
      <div className={styles.card} data-testid={`template-card-${template.id}`}>
        <div className={styles.cardHead}>
          <div>
            <p className={styles.cardName}>{template.name}</p>
            <p className={styles.cardDisplayId}>{template.display_id}</p>
          </div>
          <span
            className={`${styles.scopeChip} ${
              template.scope === "public" ? styles.scopeChipPublic : ""
            }`}
            data-testid={`template-scope-${template.id}`}
          >
            {SCOPE_LABEL[template.scope]}
          </span>
        </div>

        <div className={styles.cardBody}>
          <div className={styles.cardRow}>
            <Icon name={type?.icon ?? "rect"} size={12} />
            <span>{type?.label ?? template.type_label}</span>
          </div>
          <div className={styles.cardRow}>
            <span>{template.classes.length} 个类别</span>
            <span>·</span>
            <span>使用 {template.usage_count} 次</span>
            {template.annotation_guide ? (
              <>
                <span>·</span>
                <Icon name="book" size={11} />
                <span>含指引</span>
              </>
            ) : null}
          </div>
          {template.description ? <div>{template.description}</div> : null}
        </div>

        <div className={styles.cardActions}>
          <Button size="sm" onClick={onApply} data-testid={`template-apply-${template.id}`}>
            应用
          </Button>
          <Button size="sm" variant="ghost" onClick={onDuplicate}>
            克隆
          </Button>
          {canEdit ? (
            <>
              <Button size="sm" variant="ghost" onClick={onEdit}>
                编辑
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={onDelete}
                data-testid={`template-delete-${template.id}`}
              >
                删除
              </Button>
            </>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
