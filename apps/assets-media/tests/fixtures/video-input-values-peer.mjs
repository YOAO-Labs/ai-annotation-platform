import { createHash } from "node:crypto";

try {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 2 * 1024 * 1024) throw new Error("budget");
  }
  const { plan, entries } = JSON.parse(input);
  const video = plan.input;
  const sampling = plan.sampling;
  const fields = [
    "aap-assets-video-inputs/v1",
    video.input_id,
    video.job_id,
    video.actor_id,
    video.task.task_id,
    video.task.project_id,
    video.task.task_version,
    video.task.task_source_generation,
    video.task.task_source_snapshot,
    video.source.dataset_id,
    video.source.dataset_item_id,
    video.source.source_generation,
    video.source.source_snapshot,
    video.source.timetable_revision,
    video.blob.sha256,
    video.blob.byte_size,
    video.video.frame_count,
    video.video.width,
    video.video.height,
    video.video.duration_ms,
    video.video.video_stream,
    sampling.max_frames,
    sampling.step,
    sampling.width,
    sampling.format,
  ];
  for (const value of fields) {
    if (typeof value === "number" && !Number.isSafeInteger(value)) throw new Error("integer");
  }
  const seed = fields.map(String).join("\n") + "\n";
  const available =
    (BigInt(video.video.frame_count) + BigInt(sampling.step) - 1n) / BigInt(sampling.step);
  const count = Number(
    available < BigInt(sampling.max_frames) ? available : BigInt(sampling.max_frames),
  );
  if (count !== plan.selected_count || entries.length !== count) throw new Error("count");
  const timebase = createHash("sha256").update("timebase\n").update(seed);
  for (let offset = 0; offset < entries.length; offset++) {
    const entry = entries[offset];
    if (
      entry.ordinal !== offset + 1 ||
      entry.frame_index !== offset * sampling.step ||
      !Number.isSafeInteger(entry.pts_ms) ||
      typeof entry.is_keyframe !== "boolean"
    )
      throw new Error("ordinal");
    timebase.update(
      `${entry.ordinal}\n${entry.frame_index}\n${entry.pts_ms}\n${Number(entry.is_keyframe)}\n`,
    );
  }
  const timebaseDigest = timebase.digest("hex");
  const planDigest = createHash("sha256")
    .update("plan\n")
    .update(seed)
    .update(timebaseDigest + "\n")
    .digest("hex");
  if (timebaseDigest !== plan.timebase_digest || planDigest !== plan.plan_digest)
    throw new Error("digest");
  process.stdout.write(
    JSON.stringify({
      selected_count: count,
      timebase_digest: timebaseDigest,
      plan_digest: planDigest,
    }),
  );
} catch {
  process.stderr.write("video input values check failed\n");
  process.exitCode = 1;
}
