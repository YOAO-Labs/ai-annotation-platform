import assert from "node:assert/strict";
import test from "node:test";
import type { AiMaskAcceptResponse } from "../../../src/api/aiMasks";
import type { CocoRle } from "../../../src/pages/Workbench/stage/shared/geometry/maskRle";
import type { CandidateReviewCleanupRecord } from "./candidate-review-lifecycle";
import {
  inspectScribbleRound,
  isSmartScribbleRequest,
  scribbleMaskDifference,
  scribbleMaskEvidence,
  trackScribbleAcceptance,
} from "./_smart-scribble-evidence.ts";

// Golden digest and candidate ID come from yoao_protocol_v2's Python implementation.
const CONTENT_DIGEST = "31aac8090eb25a3e167242e7304df317ca8793c84d5b7f3f4700844c6942f72b";
const CANDIDATE_ID = "sha256:1e92ddc2b04a8d1037861b5689c9cf4614a80581c744e20083c66e742b5db06c";
const source = {
  task_id: "task-1",
  annotation_id: "annotation-1",
  source_version: 3,
  source_digest: "b".repeat(64),
};

function round() {
  const claims = {
    task_id: source.task_id,
    content_digest: CONTENT_DIGEST,
    prompt_revision: "live-round-1",
    prompt_source: {
      source_annotation_id: source.annotation_id,
      source_version: source.source_version,
      source_digest: source.source_digest,
    },
    accept_target: { mode: "refine" },
  };
  const receipt = () => `${Buffer.from(JSON.stringify(claims)).toString("base64url")}.test-only`;
  const candidate = {
    type: "mask",
    candidate_id: CANDIDATE_ID,
    score: 0.9,
    value: {
      rle: { encoding: "coco_rle" as const, size: [2, 3] as [number, number], counts: [1, 2, 3] },
    },
  };
  const request = {
    task_id: source.task_id,
    context: {
      type: "scribble",
      model_id: "sam3-interactive-seg",
      output_geometry: "mask",
      multimask_output: false,
      prompt_revision: "live-round-1",
      mask_prompt_source: {
        annotation_id: source.annotation_id,
        source_version: source.source_version,
      },
      scribbles: [
        {
          polarity: 1,
          points: [
            [0.2, 0.3],
            [0.3, 0.4],
          ],
        },
      ],
    },
  };
  const response = {
    result: [candidate],
    score: 0.9,
    model_version: "sam3",
    inference_time_ms: 12,
    output_geometry: "mask" as const,
    prompt_revision: "live-round-1",
    mask_input_next: "private-session-token",
    routing: {
      requested_backend_id: "backend-1",
      backend_pool_id: "pool-1",
      backend_instance_id: "instance-1",
      model_id: "sam3-interactive-seg",
    },
    prompt_summary: {
      family: "scribble" as const,
      positive_points: 0,
      negative_points: 0,
      boxes: 0,
      positive_scribbles: 1,
      negative_scribbles: 0,
      multimask: false,
      parameters_digest: null,
    },
    accept_receipts: { [CANDIDATE_ID]: receipt() },
  };
  return { claims, receipt, request, response };
}

test("live scribble evidence agrees with the protocol digest and omits private tokens", () => {
  const { request, response } = round();
  const { evidence } = inspectScribbleRound(request, response, source, "backend-1", [1]);
  assert.equal(evidence.mask.content_digest, CONTENT_DIGEST);
  assert.equal(evidence.mask.area, 2);
  assert.deepEqual(evidence.mask.samples, [
    [1 / 6, 0.75],
    [0.5, 0.25],
  ]);
  assert.equal(evidence.candidate_id, CANDIDATE_ID);
  assert.equal(evidence.model_version, "sam3");
  assert.ok(!JSON.stringify(evidence).includes(response.mask_input_next));
  assert.ok(!JSON.stringify(evidence).includes(response.accept_receipts[CANDIDATE_ID]));
});

test("a fixture model cannot pass the live inference gate", () => {
  const { request, response } = round();
  response.model_version = "screenshot-sam3-fixture";
  assert.throws(() => inspectScribbleRound(request, response, source, "backend-1", [1]));
});

test("warmup and other model requests cannot count as a scribble round", () => {
  const { request } = round();
  assert.equal(isSmartScribbleRequest(request), true);
  assert.equal(
    isSmartScribbleRequest({
      task_id: source.task_id,
      context: { type: "point", points: [[0.5, 0.5]], labels: [1] },
    }),
    false,
  );
  assert.equal(
    isSmartScribbleRequest({ ...request, context: { ...request.context, type: "point" } }),
    false,
  );
  assert.equal(
    isSmartScribbleRequest({
      ...request,
      context: { ...request.context, model_id: "other-model" },
    }),
    false,
  );
});

test("wrong task, backend, prompt source, or polarity cannot become provenance", () => {
  for (const mutate of [
    (r: ReturnType<typeof round>) => {
      r.request.task_id = "other-task";
    },
    (r: ReturnType<typeof round>) => {
      r.response.routing.requested_backend_id = "other-backend";
    },
    (r: ReturnType<typeof round>) => {
      r.request.context.mask_prompt_source.annotation_id = "other-mask";
    },
    (r: ReturnType<typeof round>) => {
      r.request.context.mask_prompt_source.source_version += 1;
    },
    (r: ReturnType<typeof round>) => {
      r.request.context.scribbles[0].polarity = 0;
    },
  ]) {
    const value = round();
    mutate(value);
    assert.throws(() =>
      inspectScribbleRound(value.request, value.response, source, "backend-1", [1]),
    );
  }
});

test("receipt source digest and version must still match the stored input", () => {
  for (const mutate of [
    (r: ReturnType<typeof round>) => {
      r.claims.prompt_source.source_digest = "c".repeat(64);
    },
    (r: ReturnType<typeof round>) => {
      r.claims.prompt_source.source_version += 1;
    },
    (r: ReturnType<typeof round>) => {
      r.claims.accept_target.mode = "create";
    },
  ]) {
    const value = round();
    mutate(value);
    value.response.accept_receipts[CANDIDATE_ID] = value.receipt();
    assert.throws(() =>
      inspectScribbleRound(value.request, value.response, source, "backend-1", [1]),
    );
  }
});

test("changed candidate pixels cannot reuse an old candidate ID", () => {
  const { request, response } = round();
  response.result[0].value.rle.counts = [2, 2, 2];
  assert.throws(() => inspectScribbleRound(request, response, source, "backend-1", [1]));
});

test("empty or malformed Mask content cannot pass visual evidence preparation", () => {
  for (const counts of [[6], [1, 2], [1, -1, 6]]) {
    assert.throws(() => scribbleMaskEvidence({ encoding: "coco_rle", size: [2, 3], counts }));
  }
});

test("round differences exclude shared foreground and detect a stale previous bitmap", () => {
  const previous: CocoRle = { encoding: "coco_rle", size: [2, 3], counts: [0, 3, 3] };
  const current: CocoRle = { ...previous, counts: [1, 3, 2] };
  const difference = scribbleMaskDifference(previous, current);
  assert.deepEqual(difference, { added: [[0.5, 0.75]], removed: [[1 / 6, 0.25]] });
  const bitmapMatches = (pixels: number[]) => {
    const isForeground = ([x, y]: [number, number]) =>
      pixels[Math.floor(x * 3) * 2 + Math.floor(y * 2)] === 1;
    return (
      difference.added.every(isForeground) && difference.removed.every((p) => !isForeground(p))
    );
  };
  assert.equal(bitmapMatches([1, 1, 1, 0, 0, 0]), false);
  assert.equal(bitmapMatches([0, 1, 1, 1, 0, 0]), true);
});

test("pure additions and removals keep their expected foreground polarity", () => {
  const smaller: CocoRle = { encoding: "coco_rle", size: [2, 3], counts: [0, 3, 3] };
  const larger: CocoRle = { ...smaller, counts: [0, 4, 2] };
  assert.deepEqual(scribbleMaskDifference(smaller, larger), {
    added: [[0.5, 0.75]],
    removed: [],
  });
  assert.deepEqual(scribbleMaskDifference(larger, smaller), {
    added: [],
    removed: [[0.5, 0.75]],
  });
});

test("difference sampling spans large changes and handles zero-length runs", () => {
  const previous: CocoRle = { encoding: "coco_rle", size: [20, 10], counts: [0, 50, 150] };
  const current: CocoRle = { ...previous, counts: [100, 25, 0, 75] };
  const difference = scribbleMaskDifference(previous, current);
  assert.equal(difference.added.length, 16);
  assert.equal(difference.removed.length, 16);
  assert.ok(difference.added.every(([x, y]) => x > 0.5 && y > 0 && y < 1));
  assert.ok(difference.removed.every(([x, y]) => x < 0.3 && y > 0 && y < 1));
  assert.ok(difference.added[0][0] < difference.added.at(-1)![0]);
});

test("identical pixels, changed dimensions, and invalid RLE cannot produce difference evidence", () => {
  const previous: CocoRle = { encoding: "coco_rle", size: [2, 3], counts: [0, 3, 3] };
  for (const current of [
    previous,
    { ...previous, counts: [0, 1, 0, 2, 3] },
    { ...previous, size: [3, 2] as [number, number] },
    { ...previous, counts: [0, 2, 3] },
  ]) {
    assert.throws(() => scribbleMaskDifference(previous, current));
  }
});

test("unexpected saved IDs remain cleanable when in-place acceptance validation fails", () => {
  const cleanup: CandidateReviewCleanupRecord = {
    projectId: "project-1",
    taskId: source.task_id,
    annotationIds: [source.annotation_id],
    predictionIds: [],
  };
  const response = {
    annotation: { id: "unexpected-create" },
    prediction: { id: "prediction-1" },
  } as AiMaskAcceptResponse;
  assert.throws(() => {
    trackScribbleAcceptance(response, cleanup);
    assert.equal(response.annotation.id, source.annotation_id);
  });
  trackScribbleAcceptance(response, cleanup);
  assert.deepEqual(cleanup.annotationIds, [source.annotation_id, "unexpected-create"]);
  assert.deepEqual(cleanup.predictionIds, ["prediction-1"]);
});

// The API computes prompt_revision after hydrating the stored Mask and session.
test("server-generated revisions do not require a browser-supplied revision", () => {
  const { request, response, claims, receipt } = round();
  const { prompt_revision: _unused, ...context } = request.context;
  inspectScribbleRound({ ...request, context }, response, source, "backend-1", [1]);
  claims.prompt_revision = "unrelated-round";
  response.accept_receipts[CANDIDATE_ID] = receipt();
  assert.throws(() =>
    inspectScribbleRound({ ...request, context }, response, source, "backend-1", [1]),
  );
});
