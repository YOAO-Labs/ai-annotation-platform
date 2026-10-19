import { createHash } from "node:crypto";

let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const v = JSON.parse(raw);
const hash = (domain, fields) => {
  const h = createHash("sha256").update(domain + "\n");
  for (let field of fields) {
    if (field === null) h.update("N;");
    else {
      if (typeof field === "boolean") field = Number(field);
      const bytes = Buffer.from(String(field), "utf8");
      h.update(`S${bytes.length}:`).update(bytes).update(";");
    }
  }
  return h.digest("hex");
};
const schema = "aap-assets-video-input-execution/v1";
const s = v.selection,
  f = s.sampling;
let selection = hash("selection", [
  schema,
  v.scope.job_id,
  v.scope.actor_id,
  v.scope.project_id,
  s.batch_id,
  s.execution_scope,
  s.predict_mode,
  f.max_frames,
  f.step,
  f.width,
  f.format,
  s.task_ids === null ? null : s.task_ids.length,
  ...(s.task_ids ?? []),
  v.candidate_count,
  v.selected_count,
]);
for (const m of v.captures) {
  const t = m.task,
    a = m.source,
    i = a?.object_identity;
  selection = hash("member", [
    selection,
    m.ordinal,
    t.task_id,
    t.project_id,
    t.batch_id,
    t.task_version,
    t.task_source_generation,
    t.dataset_item_id,
    t.task_source_snapshot,
    t.file_path,
    t.file_type,
    a?.dataset_item_id ?? null,
    a?.dataset_id ?? null,
    a?.source_generation ?? null,
    a?.source_snapshot ?? null,
    a?.timetable_revision ?? null,
    i?.etag ?? null,
    i?.byte_size ?? null,
    i?.version_id ?? null,
    a?.source_drifted ?? null,
    a?.file_path ?? null,
    a?.file_type ?? null,
    a?.file_size ?? null,
    a?.content_hash ?? null,
    m.error,
  ]);
}
let sealed = hash("sealed", [
  schema,
  v.header.job_id,
  v.header.actor_id,
  v.header.project_id,
  selection,
  v.header.selected_count,
  v.ready_count,
  v.failed_count,
]);
for (const m of v.members) {
  const r = m.input,
    p = m.state === "ready" ? r?.plan : null;
  sealed = hash("outcome", [
    sealed,
    m.ordinal,
    m.task.task_id,
    m.state,
    m.error,
    r?.input_id ?? null,
    p?.plan_digest ?? null,
    p?.timebase_digest ?? null,
    p?.input.blob.sha256 ?? null,
    p?.input.blob.byte_size ?? null,
    p?.selected_count ?? null,
  ]);
}
process.stdout.write(JSON.stringify({ selection_digest: selection, sealed_digest: sealed }));
