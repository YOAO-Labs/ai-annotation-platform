// Explicitly enrolled flows only: unknown flows must not acquire a manual fallback.
const manual = [
  "project-onboarding",
  "bbox-draw",
  "workspace-layout-basics",
  "rotated-bbox",
  "polyline-draw",
  "polygon-draw",
  "mask-draw",
  "ai-prediction-import",
  "review-reject",
  "pipeline-apply-project",
  "pipeline-template-create",
  "ai-pre-variant-selector",
  "model-market-runtime-pool",
  "model-market-video-pool",
  "model-market-runtime-partial-failure",
  "model-market-gpu-resource-overview",
  "ai-preannotate",
  "project-ml-routing",
  "batch-bulk-actions",
  "video-track",
  "video-timeline-zoom",
  "video-chapter",
  "video-track-carryover",
  "video-mask-track-edit",
  "video-draw",
  "video-tracker-job-states",
  "workspace-layout-persistence",
  "workspace-named-preset",
  "polygon-boundary-trace",
  "polygon-slice-undo",
  "mask-slice-instances",
  "discussion-target-drafts",
  "task-drawing-comment",
  "pointcloud-controls",
  "pointcloud-view",
  "pointcloud-billboard-label",
  "pointcloud-camera-seed-3d-box",
  "pointcloud-crossframe-track",
  "pointcloud-panel-layout",
  "large-image-progressive",
  "large-image-pyramid-recovery",
  "large-image-mask-limit",
  "hotkey-cheatsheet",
  "video-comment-badges",
  "discussion-mention-return",
  "video-issue-resolution",
  "project-guide-autosave",
  "project-invitation-join",
  "project-create-existing-resources",
  "project-actions-menu",
  "background-export-download",
  "storage-connector-create-test",
];

// Inference execution is explicit: a panel can require live capabilities without
// submitting a prediction or tracker job during its recording.
const liveInference = {
  "detection-online-training": [],
  "sam-tool-smart-point": ["image_interactive"],
  "sam-tool-smart-box": ["image_interactive"],
  "sam-tool-exemplar": ["image_interactive"],
  "sam-interactive": ["image_interactive"],
  "ocr-inference": ["ocr"],
  "secondary-inference-attribute": ["ocr"],
  "jobs-retry-recovery": ["ocr"],
  // These flows enable and execute live YOLO/ONNX jobs after their own project
  // setup. Their capability scope is intentionally empty because the flow
  // selects the backend itself; recordingInference still marks execution live.
  "ai-preannotate": [],
  "pipeline-apply-project": [],
  "current-task-image-inference": ["ocr"],
  "candidate-keyboard-review": ["image_interactive"],
  "candidate-review-lifecycle": ["image_interactive"],
  "smart-scribble": ["image_interactive"],
  "video-tracker-range": ["video_tracker"],
  "video-tracker-cross-frame-points": ["video_tracker"],
  "video-tracker-positive-negative": ["video_tracker"],
  "video-tracker-box-seed": ["video_tracker"],
  "video-tracker-text-discovery": ["video_tracker"],
  "current-frame-video-inference": ["video_tracker"],
  "video-timeline-prediction-navigation": ["video_tracker"],
  "video-mask-correction-propagate": ["video_tracker"],
  "video-propagate-track-vs-copy": ["video_tracker"],
  "video-track-batch-propagate": ["video_tracker"],
  "video-tracker-combo-discovery": ["video_tracker"],
};

export const RECORDING_FLOWS = {
  ...Object.fromEntries(manual.map((id) => [id, []])),
  ...liveInference,
  "ai-tracker-panel": ["video_tracker"],
  "project-ml-routing": ["image_interactive"],
};

export function recordingInference(flowId) {
  if (!Object.hasOwn(RECORDING_FLOWS, flowId))
    throw new Error(`Unregistered recording flow: ${flowId}`);
  return Object.hasOwn(liveInference, flowId) ? "live" : "none";
}

export const MARKETING_ONLY_FLOWS = [
  "pointcloud-controls",
  "pointcloud-view",
  "pointcloud-billboard-label",
  "pointcloud-camera-seed-3d-box",
  "pointcloud-crossframe-track",
  "pointcloud-panel-layout",
];

// Repeatable non-ML refresh selection; this does not restrict other qualified flows.
export const MAC_MARKETING_FLOWS = [
  "project-onboarding",
  "bbox-draw",
  "workspace-layout-basics",
  "rotated-bbox",
  "polyline-draw",
  "polygon-draw",
  "mask-draw",
  "review-reject",
  "batch-bulk-actions",
  "video-track",
  "video-timeline-zoom",
  "video-chapter",
  "video-track-carryover",
  "video-mask-track-edit",
  "video-draw",
  "workspace-layout-persistence",
  "workspace-named-preset",
  "polygon-boundary-trace",
  "polygon-slice-undo",
  "mask-slice-instances",
  "discussion-target-drafts",
  "task-drawing-comment",
  "pointcloud-controls",
  "pointcloud-view",
  "pointcloud-billboard-label",
  "pointcloud-camera-seed-3d-box",
  "pointcloud-crossframe-track",
  "pointcloud-panel-layout",
  "large-image-progressive",
  "large-image-pyramid-recovery",
  "large-image-mask-limit",
  "hotkey-cheatsheet",
  "video-comment-badges",
  "discussion-mention-return",
  "video-issue-resolution",
  "project-guide-autosave",
  "project-invitation-join",
  "project-create-existing-resources",
  "project-actions-menu",
  "background-export-download",
  "storage-connector-create-test",
];

export function recordingPlan(flows, profile = "marketing") {
  if (!["docs", "marketing"].includes(profile)) throw new Error(`Unknown profile: ${profile}`);
  if (!flows.length)
    throw new Error("Select at least one --flow; use --list to see supported flows.");
  for (const flow of flows) {
    if (!Object.hasOwn(RECORDING_FLOWS, flow))
      throw new Error(`Unregistered recording flow: ${flow}`);
    if (profile !== "marketing" && MARKETING_ONLY_FLOWS.includes(flow)) {
      throw new Error(
        `${flow} requires --profile marketing (hardware WebGL/60Hz); no portable fallback`,
      );
    }
  }
  const selected = [...new Set(flows)];
  const requirements = [...new Set(selected.flatMap((id) => RECORDING_FLOWS[id]))].sort();
  return {
    flows: selected,
    profile,
    backendRequirements: requirements.join(",") || "none",
    // Match the flow prefix, not a substring in the title or source filename.
    grep: `(?:^| )(${selected.join("|")}) —`,
  };
}

export function marketingCaptureDriver(platform, requestedDriver, flows = []) {
  const driver = requestedDriver ?? (platform === "darwin" ? "screencapturekit" : "x11grab");
  if (platform === "darwin" && driver === "screencapturekit") {
    const plan = recordingPlan(flows, "marketing");
    if (
      plan.backendRequirements !== "none" ||
      plan.flows.some((id) => recordingInference(id) !== "none")
    ) {
      throw new Error("Mac marketing capture requires flows with no ML capabilities or inference.");
    }
    return driver;
  }
  if (platform === "linux" && ["x11grab", "gpu-screen-recorder"].includes(driver)) return driver;
  throw new Error(`Unsupported marketing capture platform/driver: ${platform}/${driver}`);
}

export function screenshotCatalogPath(scope = process.env.SCREENSHOT_BACKEND_REQUIREMENTS) {
  return (
    "/api/v1/__test/seed/catalog?profile=screenshots" +
    (scope === undefined ? "" : `&backend_requirements=${encodeURIComponent(scope)}`)
  );
}

// Standard capture is rehearsal only; production sources must pass marketing gates.
export function assertRecordingMode(profile, validateOnly) {
  if (profile === "docs" && !validateOnly) {
    throw new Error(
      "The docs profile is rehearsal only; use --validate-only or capture a marketing master.",
    );
  }
}

// Selected captures include isolated fixture reconstruction and master encoding.
export function recordingTimeout(requested, current, selected) {
  return selected ? Math.max(900_000, current, requested) : requested;
}
