// CreateProjectWizard 第 5 步: 数据集关联 + 批次切分.
// 从 CreateProjectWizard.tsx 抽出.

import { useEffect, useState } from "react";
import { clsx } from "clsx";
import { Button } from "@/components/ui/Button";
import { Icon } from "@/components/ui/Icon";
import { useToastStore } from "@/components/ui/Toast";
import { useDatasets } from "@/hooks/useDatasets";
import { useSplitBatches } from "@/hooks/useBatches";
import { LinkJobProgress } from "@/components/datasets/LinkJobProgress";
import type { DatasetResponse } from "@/api/datasets";
import type { ProjectResponse } from "@/api/projects";
import type { FormState } from "../CreateProjectWizard";
import styles from "../CreateProjectWizard.module.css";

export function Step5Datasets({
  project,
  form,
  setForm,
  onNext,
}: {
  project: ProjectResponse;
  form: FormState;
  setForm: React.Dispatch<React.SetStateAction<FormState>>;
  onNext: (linked: number) => void;
}) {
  const pushToast = useToastStore((s) => s.push);
  const projectHasScenes = !!project.scene_mode;
  // data_type 传项目原始值,后端按 media kind 归一(lidar/point_cloud 互通),
  // 与 link 硬门同口径,避免存成 "lidar" 的点云数据集被 picker 漏掉。
  const { data: datasetsRes, isLoading } = useDatasets({
    data_type: project.data_type ?? "image",
    has_scenes: projectHasScenes,
  });
  const splitMutation = useSplitBatches(project.id);
  // useLinkProject 需要 datasetId 维度的实例，链路上每个 ds 的 mutation 都建一个会失控；
  // 这里走原始 api 直接调（hooks 仅用于 invalidate；step 完成后整体 invalidate 一次足够）。
  const datasets: DatasetResponse[] = datasetsRes?.items ?? [];
  const [linking, setLinking] = useState(false);
  // 大 dataset 异步建 task 的 job id 列表，非空则在底部显示进度条
  const [linkJobIds, setLinkJobIds] = useState<string[]>([]);
  // 大 dataset 异步时 task 尚未建完，切分需延后到所有建任务 job 完成再跑
  const [splitAfterJobs, setSplitAfterJobs] = useState(false);

  const runSplit = async () => {
    if (form.splitStrategy === "none") return;
    try {
      if (form.splitStrategy === "by_scene") {
        const created = await splitMutation.mutateAsync({
          strategy: "by_scene",
          name_prefix: "Scene",
          priority: 50,
        });
        // by_scene 切到 0 batch(所选数据集未识别出 scene)时不能静默成功 —— 跨步骤
        // 切换可能让 by_scene 落在无 scene 的数据集上,给用户显式告警。
        if (!created || created.length === 0) {
          pushToast({
            msg: "未生成批次：所选数据集未识别出任何 scene",
            sub: "请确认数据集已声明 scene（时序 / 点云导入）后再按 scene 切分。",
            kind: "warning",
          });
        }
      } else {
        await splitMutation.mutateAsync({
          strategy: "random",
          n_batches: form.splitNBatches,
          name_prefix: "Batch",
          priority: 50,
        });
      }
    } catch (e) {
      pushToast({
        msg: "批次切分失败（可在设置页重试）",
        sub: (e as Error).message,
      });
    }
  };

  // 所有异步建任务 job 完成（linkJobIds 清空）后，再执行延后的切分。
  useEffect(() => {
    if (splitAfterJobs && linkJobIds.length === 0) {
      setSplitAfterJobs(false);
      void runSplit();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [splitAfterJobs, linkJobIds]);

  const toggle = (id: string) => {
    setForm((s) => ({
      ...s,
      datasetIds: s.datasetIds.includes(id)
        ? s.datasetIds.filter((x) => x !== id)
        : [...s.datasetIds, id],
    }));
  };

  const onContinue = async () => {
    if (form.datasetIds.length === 0) {
      onNext(0);
      return;
    }
    setLinking(true);
    try {
      const { datasetsApi } = await import("@/api/datasets");
      // 依次 link（保证审计一行一项），失败不阻断
      let linkedOK = 0;
      const jobIds: string[] = [];
      for (const dsId of form.datasetIds) {
        try {
          const res = await datasetsApi.linkProject(dsId, project.id);
          if (res.async_job_id) jobIds.push(res.async_job_id);
          linkedOK++;
        } catch (e) {
          pushToast({
            msg: "数据集关联失败",
            sub: (e as Error).message,
            kind: "error",
          });
        }
      }
      // 切分（仅当用户选了 >=2）。大 dataset 异步建 task 时此刻任务尚未建完，
      // 立即切分会切到空集 → 延后到所有建任务 job 完成再切（见 splitAfterJobs effect）；
      // 全同步时任务已就绪，立即切。
      if (form.splitStrategy !== "none") {
        if (jobIds.length > 0) {
          setSplitAfterJobs(true);
        } else {
          await runSplit();
        }
      }
      pushToast({ msg: `已关联 ${linkedOK} 个数据集`, kind: "success" });
      // 有大 dataset 异步建 task：留在本步显示进度，待用户点「下一步」再前进；
      // 否则（全同步）直接前进。
      if (jobIds.length > 0) {
        setLinkJobIds(jobIds);
      } else {
        onNext(linkedOK);
      }
    } finally {
      setLinking(false);
    }
  };

  return (
    <div className={styles.formStack}>
      <div className={styles.sectionHint}>
        选择要关联到本项目的数据集（可空 /
        多选）。关联后任务会作为「未归类」加入项目；可在下方选择初始分包方式。
      </div>

      {isLoading && <div className={styles.inlineLoading}>加载数据集…</div>}

      {!isLoading && datasets.length === 0 && (
        <div className={styles.emptyPanel}>
          {projectHasScenes
            ? "没有符合该项目类型且包含时序（scene）数据的数据集，请先导入对应数据集，或跳过后稍后关联。"
            : "没有符合该项目类型的数据集，请先导入对应数据集，或跳过后稍后关联。"}
        </div>
      )}

      {!isLoading && datasets.length > 0 && (
        <div className={styles.datasetList}>
          {datasets.map((d) => {
            const checked = form.datasetIds.includes(d.id);
            return (
              <button
                key={d.id}
                type="button"
                onClick={() => toggle(d.id)}
                className={clsx(styles.choiceButton, checked && styles.choiceButtonChecked)}
              >
                <span className={clsx(styles.checkMark, checked && styles.checkMarkChecked)}>
                  {checked && <Icon name="check" size={10} />}
                </span>
                <span className={styles.choiceBody}>
                  <div className={styles.choiceTitle}>{d.name}</div>
                  <div className={styles.choiceMeta}>
                    <span className="mono">{d.display_id}</span> · {d.file_count} 个文件 ·{" "}
                    {d.data_type}
                    {d.has_scenes ? " · 时序" : ""}
                  </div>
                </span>
              </button>
            );
          })}
        </div>
      )}

      {form.datasetIds.length > 0 && (
        <div className={styles.splitPanel}>
          <div className={styles.splitTitle}>关联后的初始分包</div>
          <div className={styles.splitOptions}>
            <label className={styles.radioLabel}>
              <input
                type="radio"
                checked={form.splitStrategy === "none"}
                onChange={() => setForm((s) => ({ ...s, splitStrategy: "none" }))}
              />
              暂不分包
            </label>
            {projectHasScenes && (
              <label className={styles.radioLabel}>
                <input
                  type="radio"
                  checked={form.splitStrategy === "by_scene"}
                  onChange={() => setForm((s) => ({ ...s, splitStrategy: "by_scene" }))}
                />
                按 scene 分包
              </label>
            )}
            <label className={styles.radioLabel}>
              <input
                type="radio"
                checked={form.splitStrategy === "random"}
                onChange={() =>
                  setForm((s) => ({
                    ...s,
                    splitStrategy: "random",
                    splitNBatches: Math.max(2, s.splitNBatches),
                  }))
                }
              />
              随机切分为
              <input
                type="number"
                min={2}
                max={20}
                value={form.splitStrategy === "random" ? form.splitNBatches : 3}
                disabled={form.splitStrategy !== "random"}
                onChange={(e) =>
                  setForm((s) => ({
                    ...s,
                    splitStrategy: "random",
                    splitNBatches: Math.max(2, Math.min(20, Number(e.target.value))),
                  }))
                }
                className={clsx(styles.input, styles.batchCountInput)}
              />
              个批次
            </label>
          </div>
        </div>
      )}

      {linkJobIds.length > 0 && (
        <div className={styles.linkProgressList}>
          {linkJobIds.map((jid) => (
            <LinkJobProgress
              key={jid}
              jobId={jid}
              projectId={project.id}
              onDone={() => setLinkJobIds((prev) => prev.filter((x) => x !== jid))}
            />
          ))}
        </div>
      )}

      <div className={styles.stepActions}>
        {linkJobIds.length > 0 ? (
          <Button variant="primary" onClick={() => onNext(form.datasetIds.length)}>
            下一步
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={() => onNext(0)} disabled={linking}>
              跳过
            </Button>
            <Button variant="primary" onClick={onContinue} disabled={linking}>
              {linking
                ? "关联中…"
                : form.datasetIds.length === 0
                  ? "下一步"
                  : `关联 ${form.datasetIds.length} 个并继续`}
            </Button>
          </>
        )}
      </div>
    </div>
  );
}
