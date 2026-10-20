/**
 * v0.9.12 · /ai-pre 信息架构重构 (BUG B-17).
 *
 * 主视图: 项目卡片网格 (仅接了 ml_backend 的项目) → 点卡片进 ProjectDetailPanel.
 * ProjectDetailPanel 内: 多选 batch + 串/并行预标 + 已就绪 HistoryTable (含多选清理).
 *
 * v0.10.40 · 删除 v0.9.7 遗留且无消费方的 stepper 子组件 (PreannotateStepper /
 * ProjectBatchPicker / PromptComposer / OutputModeSelector / RunPanel / usePreannotateDraft);
 * 精细单批次 modal 方向已放弃, ProjectDetailPanel 用 inline 简化版承载全部预标交互.
 * HistoryTable 仍在 ProjectDetailPanel 内渲染.
 *
 * 旧版本 478 行单文件 + 内嵌 FailedPredictionsTab (B-2) 一并清理 → /ai-pre/jobs?status=failed.
 */

import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";

import { ProjectCardGrid } from "./components/ProjectCardGrid";
import { ProjectDetailPanel } from "./components/ProjectDetailPanel";
import { useAIPreProjectSummary } from "@/hooks/useBulkPreannotateActions";
import styles from "./AIPreAnnotatePage.module.css";

export default function AIPreAnnotatePage() {
  const summaryQ = useAIPreProjectSummary();
  const items = useMemo(() => summaryQ.data?.items ?? [], [summaryQ.data?.items]);

  const [search, setSearch] = useSearchParams();
  const selectedProjectId = search.get("project");
  const setSelectedProjectId = (id: string | null) => {
    const next = new URLSearchParams(search);
    if (id) next.set("project", id);
    else {
      next.delete("project");
      next.delete("catalog_model");
    }
    setSearch(next);
  };

  const selectedSummary = useMemo(
    () => items.find((it) => it.project_id === selectedProjectId),
    [items, selectedProjectId],
  );

  return (
    <div className={styles.page}>
      <header>
        <h1 className={styles.title}>AI 预标</h1>
        <p className={styles.subtitle}>
          先选项目（仅展示已绑定 ML backend 的项目）。图像项目多选批次跑文本批量预标；视频项目的 AI
          追踪在工作台逐轨迹发起（Ctrl+B）。
        </p>
      </header>

      {selectedProjectId ? (
        <ProjectDetailPanel
          projectId={selectedProjectId}
          summary={selectedSummary}
          initialCatalogModelId={search.get("catalog_model")}
          onBack={() => setSelectedProjectId(null)}
        />
      ) : (
        <ProjectCardGrid
          items={items}
          isLoading={summaryQ.isLoading}
          onSelect={setSelectedProjectId}
        />
      )}
    </div>
  );
}
