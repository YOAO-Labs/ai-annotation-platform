import { describe, expect, it } from "vitest";

import {
  clearReviewAssignee,
  clearReviewSelection,
  readReviewQueueParams,
  readReviewTaskId,
  reviewQueueIsUnchanged,
  reviewQueueScopeKey,
  selectReviewBatch,
  setReviewTaskId,
} from "./reviewUrlState";

function params(init: string): URLSearchParams {
  return new URLSearchParams(init);
}

describe("reviewUrlState", () => {
  it("reads the queue scope and tolerates missing params", () => {
    expect(readReviewQueueParams(params("project=p1&batch=b1&assignee=u1"))).toEqual({
      project: "p1",
      batch: "b1",
      assignee: "u1",
    });
    expect(readReviewQueueParams(params(""))).toEqual({
      project: "",
      batch: "",
      assignee: "",
    });
  });

  it("changes the scope key when any owned dimension changes", () => {
    const base = { project: "p1", batch: "b1", assignee: "u1" };
    const key = reviewQueueScopeKey("owner:tok", base);
    expect(reviewQueueScopeKey("owner:tok", { ...base, batch: "b2" })).not.toBe(key);
    expect(reviewQueueScopeKey("owner:tok", { ...base, assignee: "u2" })).not.toBe(key);
    expect(reviewQueueScopeKey("owner:tok", { ...base, project: "p2" })).not.toBe(key);
    expect(reviewQueueScopeKey("other:tok", base)).not.toBe(key);
    expect(
      reviewQueueIsUnchanged(
        { authOwnerKey: "owner:tok", ...base },
        { authOwnerKey: "owner:tok", ...base },
      ),
    ).toBe(true);
    expect(
      reviewQueueIsUnchanged(
        { authOwnerKey: "owner:tok", ...base },
        { authOwnerKey: "owner:tok", ...base, batch: "b2" },
      ),
    ).toBe(false);
  });

  it("selecting a batch replaces project/batch but keeps assignee and unknown params", () => {
    const next = selectReviewBatch(params("project=old&batch=old&assignee=u1&keep=yes"), {
      project_id: "p2",
      batch_id: "b2",
    });
    expect(next.get("project")).toBe("p2");
    expect(next.get("batch")).toBe("b2");
    expect(next.get("assignee")).toBe("u1");
    expect(next.get("keep")).toBe("yes");
  });

  it("selecting null clears the batch selection without touching assignee", () => {
    const next = selectReviewBatch(params("project=p1&batch=b1&assignee=u1"), null);
    expect(next.get("project")).toBeNull();
    expect(next.get("batch")).toBeNull();
    expect(next.get("assignee")).toBe("u1");
  });

  it("clearing the assignee leaves the batch/project selection intact", () => {
    const next = clearReviewAssignee(params("project=p1&batch=b1&assignee=u1"));
    expect(next.get("project")).toBe("p1");
    expect(next.get("batch")).toBe("b1");
    expect(next.get("assignee")).toBeNull();
  });

  it("back to overview drops every queue-scope param", () => {
    const next = clearReviewSelection(params("project=p1&batch=b1&assignee=u1&keep=yes"));
    expect(next.get("project")).toBeNull();
    expect(next.get("batch")).toBeNull();
    expect(next.get("assignee")).toBeNull();
    expect(next.get("keep")).toBe("yes");
  });

  it("opens and closes the task drawer without dropping queue scope", () => {
    const opened = setReviewTaskId(params("project=p1&batch=b1"), "t1");
    expect(readReviewTaskId(opened)).toBe("t1");
    expect(opened.get("batch")).toBe("b1");
    const closed = setReviewTaskId(opened, null);
    expect(readReviewTaskId(closed)).toBeNull();
    expect(closed.get("project")).toBe("p1");
  });
});
