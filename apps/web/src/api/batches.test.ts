import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ExportTarget } from "./projects";

const get = vi.fn((..._a: unknown[]) => Promise.resolve({}));
const post = vi.fn((..._a: unknown[]) => Promise.resolve({}));
const patch = vi.fn((..._a: unknown[]) => Promise.resolve({}));
const del = vi.fn((..._a: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    patch: (...a: unknown[]) => patch(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));

import { batchesApi } from "./batches";

beforeEach(() => {
  get.mockClear();
  post.mockClear();
  patch.mockClear();
  del.mockClear();
});

describe("batchesApi · endpoint 契约", () => {
  it("list（带/不带 status）", () => {
    batchesApi.list("p1");
    expect(get).toHaveBeenCalledWith("/projects/p1/batches");
    batchesApi.list("p1", "active");
    expect(get).toHaveBeenCalledWith("/projects/p1/batches?status=active");
  });

  it("get / create / update", () => {
    batchesApi.get("p1", "b1");
    expect(get).toHaveBeenCalledWith("/projects/p1/batches/b1");
    batchesApi.create("p1", { name: "n" });
    expect(post).toHaveBeenCalledWith("/projects/p1/batches", { name: "n" });
    batchesApi.update("p1", "b1", { priority: 3 });
    expect(patch).toHaveBeenCalledWith("/projects/p1/batches/b1", { priority: 3 });
  });

  it("remove（force 与否）", () => {
    batchesApi.remove("p1", "b1");
    expect(del).toHaveBeenCalledWith("/projects/p1/batches/b1");
    batchesApi.remove("p1", "b1", true);
    expect(del).toHaveBeenCalledWith("/projects/p1/batches/b1?force=true");
  });

  it("transition（带/不带 reason）", () => {
    batchesApi.transition("p1", "b1", "active");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/transition", {
      target_status: "active",
    });
    batchesApi.transition("p1", "b1", "active", "go");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/transition", {
      target_status: "active",
      reason: "go",
    });
  });

  it("split / reject / reset", () => {
    batchesApi.split("p1", { strategy: "random", n_batches: 3 });
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/split", {
      strategy: "random",
      n_batches: 3,
    });
    batchesApi.reject("p1", "b1", "fb");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/reject", { feedback: "fb" });
    batchesApi.reset("p1", "b1", "reason10char");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/reset", {
      reason: "reason10char",
    });
  });

  it("submitReview 整批送审", () => {
    batchesApi.submitReview("p1", "b1");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/submit-review", {});
  });

  it("distribute / bulk 操作", () => {
    batchesApi.distributeBatches("p1", { only_unassigned: true });
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/distribute-batches", {
      only_unassigned: true,
    });
    batchesApi.bulkArchive("p1", ["b1", "b2"]);
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-archive", {
      batch_ids: ["b1", "b2"],
    });
    batchesApi.bulkDelete("p1", ["b1"]);
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-delete", {
      batch_ids: ["b1"],
    });
    batchesApi.bulkDelete("p1", ["b1"], true);
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-delete?force=true", {
      batch_ids: ["b1"],
    });
    batchesApi.bulkReassign("p1", { batch_ids: ["b1"], annotator_id: "u1" });
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-reassign", {
      batch_ids: ["b1"],
      annotator_id: "u1",
    });
    batchesApi.bulkActivate("p1", ["b1"]);
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-activate", {
      batch_ids: ["b1"],
    });
    batchesApi.bulkApprove("p1", ["b1"]);
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-approve", {
      batch_ids: ["b1"],
    });
    batchesApi.bulkReject("p1", ["b1"], "fb");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/bulk-reject", {
      batch_ids: ["b1"],
      feedback: "fb",
    });
  });

  it("distribute preview carries the same assignment scope", () => {
    batchesApi.previewDistribution("p1", {
      annotator_ids: ["u1"],
      reviewer_ids: ["u2"],
      only_unassigned: true,
    });
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/distribution-preview", {
      annotator_ids: ["u1"],
      reviewer_ids: ["u2"],
      only_unassigned: true,
    });
    batchesApi.applyDistribution("p1", {
      annotator_ids: ["u1"],
      reviewer_ids: ["u2"],
      only_unassigned: true,
      preview_version: "preview-1",
    });
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/distribution-apply", {
      annotator_ids: ["u1"],
      reviewer_ids: ["u2"],
      only_unassigned: true,
      preview_version: "preview-1",
    });
  });

  it("admin lock / unlock", () => {
    batchesApi.adminLock("p1", "b1", "why");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/admin-lock", { reason: "why" });
    batchesApi.adminUnlock("p1", "b1");
    expect(post).toHaveBeenCalledWith("/projects/p1/batches/b1/admin-unlock", {});
  });

  it("auditLogs（默认/自定义 limit）/ unclassifiedCount", () => {
    batchesApi.auditLogs("p1", "b1");
    expect(get).toHaveBeenCalledWith("/projects/p1/batches/b1/audit-logs?limit=50");
    batchesApi.auditLogs("p1", "b1", 10);
    expect(get).toHaveBeenCalledWith("/projects/p1/batches/b1/audit-logs?limit=10");
    batchesApi.unclassifiedCount("p1");
    expect(get).toHaveBeenCalledWith("/projects/p1/batches/unclassified-count");
  });

  it("exportBatch 拼 targets / include_attributes / video_frame_mode", () => {
    batchesApi.exportBatch("p1", "b1", ["coco", "yolo-det"] as ExportTarget[], {
      videoFrameMode: "keyframes",
    });
    expect(post).toHaveBeenCalledWith(
      "/projects/p1/batches/b1/export?include_attributes=true&targets=coco&targets=yolo-det&video_frame_mode=keyframes",
      {},
    );
    batchesApi.exportBatch("p1", "b1", ["coco"] as ExportTarget[], {
      includeAttributes: false,
    });
    expect(post).toHaveBeenCalledWith(
      "/projects/p1/batches/b1/export?include_attributes=false&targets=coco",
      {},
    );
    batchesApi.exportBatch("p1", "b1", ["davis", "mots"] as ExportTarget[], {
      includeAttributes: true,
      videoOverlapPolicy: "z_order",
      motsFrameBase: 1,
    });
    expect(post).toHaveBeenCalledWith(
      "/projects/p1/batches/b1/export?include_attributes=true&targets=davis&targets=mots" +
        "&video_overlap_policy=z_order&mots_frame_base=1",
      {},
    );
  });
});
