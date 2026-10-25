import type { AsyncJob, StorageJobProgressObservation } from "@/api/asyncJobs";

const PAIRS: Record<string, Record<string, readonly string[]>> = {
  dataset_scan: { scanning: ["directories", "pages"], validating: ["directories", "pages"] },
  dataset_registration: { preflight: ["entries"], registering: ["entries"] },
  storage_usage: { enumerating: ["pages"] },
  storage_migration_inventory: { enumerating: ["records", "pages"], publishing: ["records"] },
  storage_migration_copy: {
    preparing: ["objects"],
    copying: ["objects"],
    verifying: ["objects"],
    publishing: ["objects"],
  },
};
function permittedUnits(kind: string, stage: string) {
  if (!Object.prototype.hasOwnProperty.call(PAIRS, kind)) return undefined;
  const stages = PAIRS[kind];
  return Object.prototype.hasOwnProperty.call(stages, stage) ? stages[stage] : undefined;
}
const STAGES: Record<StorageJobProgressObservation["stage"], string> = {
  scanning: "扫描目录",
  validating: "校验扫描结果",
  preflight: "登记预检",
  registering: "登记样本",
  enumerating: "读取清单",
  preparing: "准备复制计划",
  copying: "复制对象",
  verifying: "核验对象",
  publishing: "发布结果",
};
const UNITS: Record<StorageJobProgressObservation["unit"], string> = {
  directories: "个目录",
  pages: "页",
  entries: "条登记项",
  records: "条清单记录",
  objects: "个对象",
};
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
function timestamp(value: unknown): number | null {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T/.test(value)) return null;
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : null;
}
function duration(seconds: number) {
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${hours} 小时${minutes ? ` ${minutes} 分钟` : ""}`;
}

/** Public observations are untrusted at runtime, including historical responses. */
export function storageProgressObservation(job: AsyncJob): StorageJobProgressObservation | null {
  const raw: unknown = job.progress_observation;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const value = raw as Record<string, unknown>;
  if (
    value.schema !== 1 ||
    typeof value.execution_id !== "string" ||
    !UUID.test(value.execution_id) ||
    typeof value.stage !== "string" ||
    typeof value.unit !== "string" ||
    !permittedUnits(job.kind, value.stage)?.includes(value.unit) ||
    !Number.isSafeInteger(value.completed) ||
    Number(value.completed) < 0 ||
    !Number.isSafeInteger(value.sequence) ||
    Number(value.sequence) <= 0 ||
    value.attention_after_seconds !== 900 ||
    !Number.isSafeInteger(value.idle_seconds) ||
    Number(value.idle_seconds) < 0
  )
    return null;
  const recorded = timestamp(job.last_progress_at);
  const observed = timestamp(value.observed_at);
  const created = timestamp(job.created_at);
  const started = timestamp(job.started_at);
  if (
    recorded === null ||
    observed === null ||
    created === null ||
    recorded < created ||
    recorded > observed ||
    (started !== null && recorded < started) ||
    Math.abs(Math.floor((observed - recorded) / 1000) - Number(value.idle_seconds)) > 1
  )
    return null;
  return raw as StorageJobProgressObservation;
}
function elapsed(job: AsyncJob, observation: StorageJobProgressObservation | null) {
  const created = timestamp(job.created_at);
  const started = timestamp(job.started_at);
  const completed = timestamp(job.completed_at);
  const terminal = !["pending", "running"].includes(job.status);
  const serverNow = timestamp(observation?.observed_at);
  const end = terminal ? completed : (serverNow ?? Date.now());
  const start = job.status === "pending" ? created : started;
  const label = job.status === "pending" ? "已等待" : terminal ? "本次执行" : "已执行";
  if (
    start === null ||
    end === null ||
    end < start ||
    created === null ||
    (started !== null && started < created)
  )
    return `${label}时长未知`;
  return `${label}${!terminal && serverNow === null ? "约 " : " "}${duration((end - start) / 1000)}${!terminal && serverNow === null ? "（本机估计）" : ""}`;
}
export function StorageJobProgress({ job }: { job: AsyncJob }) {
  if (!Object.prototype.hasOwnProperty.call(PAIRS, job.kind)) return null;
  const observation = storageProgressObservation(job);
  const waiting = job.status === "pending";
  const attention =
    job.status === "running" &&
    !!observation &&
    observation.idle_seconds !== null &&
    observation.idle_seconds >= 900;
  const manager = ["dataset_scan", "dataset_registration"].includes(job.kind)
    ? "数据集管理员"
    : "超级管理员";
  return (
    <section
      aria-label="已提交进展"
      className="space-y-2 rounded-md border border-border p-3 text-sm"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-sm font-medium">已提交进展</h3>
        <span className="text-xs text-muted-foreground tabular-nums">
          {elapsed(job, observation)}
        </span>
      </div>
      {waiting ? (
        <p className="text-xs leading-relaxed text-muted-foreground">
          等待执行；尚不能确认本轮已提交进展。
        </p>
      ) : observation ? (
        <>
          <p className="tabular-nums">
            {STAGES[observation.stage]} · 已提交 {observation.completed.toLocaleString("zh-CN")}{" "}
            {UNITS[observation.unit]}
          </p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            最后记录推进：{new Date(job.last_progress_at!).toLocaleString("zh-CN")}。
            {job.status === "running" ? "按本次服务器观测显示。" : "保留结束前的最后记录。"}
          </p>
        </>
      ) : (
        <p className="text-xs leading-relaxed text-muted-foreground">
          尚无可核对的已提交进展。可能尚未产生首条记录、正在准备，或此任务没有有效观测；不能由百分比、更新时间或心跳推断。
        </p>
      )}
      {attention && (
        <p role="status" className="text-xs leading-relaxed text-status-caution">
          已 {duration(observation.idle_seconds!)}{" "}
          没有新的已提交进展，请核实来源、队列或执行节点。这不表示任务失败、死锁或已经停止。
        </p>
      )}
      <p className="text-xs leading-relaxed text-muted-foreground">
        处置角色：{manager}核对{manager === "数据集管理员" ? "来源与权限" : "存储操作"}
        ，运维核对队列与 Worker。
        {waiting ? "等待时长不代表执行时长。" : "观测只反映已记录推进，不保证底层操作已收尾。"}
      </p>
    </section>
  );
}
