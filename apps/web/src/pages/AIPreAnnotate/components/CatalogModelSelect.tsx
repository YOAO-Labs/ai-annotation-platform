import { useId } from "react";
import { classSourceLabel } from "@/api/modelCatalog";
import type { PreannotateConfig } from "./usePreannotateConfig";
import styles from "./ProjectDetailPanel.module.css";

export function CatalogModelSelect({ cfg }: { cfg: PreannotateConfig }) {
  const id = useId();
  const release = cfg.catalogQ?.data?.current_release;
  const current = cfg.catalogModels?.find((entry) => entry.id === release?.entry_id);
  return (
    <div className={styles.field}>
      <label className={styles.fieldLabel} htmlFor={id}>
        项目检测模型
      </label>
      <select
        id={id}
        className={styles.promptInput}
        value={cfg.selectedCatalogId ?? ""}
        onChange={(event) => cfg.setCatalogModel(event.target.value)}
      >
        <option value="">保留当前后端配置</option>
        {current && <option value="project-release">项目当前发布 · {current.name}</option>}
        {cfg.catalogModels?.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.source === "project_trained" ? "项目模型" : "内置"} · {entry.name}{" "}
            {Object.values(entry.model_variants ?? {}).join(" / ")}
            {entry.publication === "candidate" ? " · 尚未发布" : ""}
          </option>
        ))}
      </select>
      {cfg.catalogQ?.isError && (
        <p role="alert" className="text-xs text-status-danger">
          读取项目模型失败，请重试。
        </p>
      )}
      {cfg.catalogReason && (
        <p role="status" className="text-xs text-status-caution">
          {cfg.catalogReason}
        </p>
      )}
      {cfg.selectedCatalogEntry && (
        <p className="text-xs text-muted-foreground">
          {classSourceLabel(cfg.selectedCatalogEntry)} ·
          原生类别过滤使用此模型的索引；父框类别过滤使用项目类别。
        </p>
      )}
    </div>
  );
}
