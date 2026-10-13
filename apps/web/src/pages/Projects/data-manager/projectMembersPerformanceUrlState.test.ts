import { describe, expect, it } from "vitest";
import {
  parseProjectMembersPerformanceUrl,
  projectMembersPerformanceUrlCodec,
  updateProjectMembersPerformanceUrl,
} from "./projectMembersPerformanceUrlState";

describe("project member performance URL state", () => {
  it("uses safe defaults and preserves unrelated Data Manager keys", () => {
    const parsed = parseProjectMembersPerformanceUrl("lens=tasks&members_work_type=review");
    expect(parsed.issues).toEqual([]);
    expect(parsed.state).toMatchObject({
      preset: "7d",
      workType: "review",
      accountStatus: "all",
      includeHistorical: false,
      sort: "name",
      direction: "asc",
      cursor: null,
      selected: null,
    });

    const next = updateProjectMembersPerformanceUrl("lens=tasks", {
      q: "Ada Lovelace",
      selected: "member/1",
      includeHistorical: true,
      sort: "current_backlog",
      direction: "desc",
    });
    expect(next.get("lens")).toBe("tasks");
    expect(next.get("members_q")).toBe("Ada Lovelace");
    expect(next.get("members_selected")).toBe("member/1");
    expect(next.get("members_historical")).toBe("1");
    expect(next.get("members_sort")).toBe("current_backlog");
    expect(next.get("members_direction")).toBe("desc");
  });

  it("rejects impossible, reversed and incomplete custom ranges", () => {
    const parsed = parseProjectMembersPerformanceUrl(
      "members_preset=custom&members_from=2026-02-30&members_to=2026-02-01",
    );
    expect(parsed.issues.map((issue) => issue.key)).toContain("members_from");
    expect(parsed.state.from).toBe("");
    expect(parsed.state.to).toBe("2026-02-01");

    const reversed = parseProjectMembersPerformanceUrl(
      "members_preset=custom&members_from=2026-09-08&members_to=2026-09-01",
    );
    expect(reversed.issues.some((issue) => issue.key === "members_range")).toBe(true);

    const incomplete = parseProjectMembersPerformanceUrl(
      "members_preset=custom&members_from=2026-09-01",
    );
    expect(incomplete.issues.some((issue) => issue.key === "members_range")).toBe(true);

    const tooLong = parseProjectMembersPerformanceUrl(
      "members_preset=custom&members_from=2026-01-01&members_to=2026-04-02",
    );
    expect(tooLong.issues.some((issue) => issue.key === "members_range")).toBe(true);
  });

  it("clears owned keys without touching other sections", () => {
    const current = new URLSearchParams(
      "section=members&lens=tasks&members_preset=30d&members_selected=u1&members_q=abc",
    );
    const cleared = projectMembersPerformanceUrlCodec.clear?.(current, undefined as never);
    expect(cleared?.toString()).toBe("section=members&lens=tasks");
  });
});
