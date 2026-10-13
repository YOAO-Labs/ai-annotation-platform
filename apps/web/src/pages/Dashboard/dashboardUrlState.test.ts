import { describe, expect, it } from "vitest";

import {
  dashboardUrlCodec,
  EMPTY_DASHBOARD_URL_STATE,
  parseDashboardUrlWithIssues,
} from "./dashboardUrlState";

describe("Dashboard URL filter state", () => {
  it("restores valid pagination and falls back for invalid values", () => {
    expect(parseDashboardUrlWithIssues("page=3&page_size=50").state).toMatchObject({
      page: 3,
      page_size: 50,
    });
    for (const query of [
      "page=0&page_size=0",
      "page=-1&page_size=500",
      "page=Infinity&page_size=bad",
    ]) {
      const parsed = parseDashboardUrlWithIssues(query);
      expect(parsed.state).toMatchObject({ page: 1, page_size: 20 });
      expect(parsed.issues.map((issue) => issue.key)).toEqual(["page", "page_size"]);
    }
    const encoded = dashboardUrlCodec.encode(new URLSearchParams("layout=grid"), {
      ...EMPTY_DASHBOARD_URL_STATE,
      page: 3,
      page_size: 100,
    });
    expect(encoded.toString()).toBe("layout=grid&page=3&page_size=100");
    expect(dashboardUrlCodec.clear!(encoded, EMPTY_DASHBOARD_URL_STATE).toString()).toBe(
      "layout=grid",
    );
  });

  it("round-trips approved keys and preserves wizard/layout parameters", () => {
    const encoded = dashboardUrlCodec.encode(
      new URLSearchParams("new=1&from=p1&layout=grid&keep=1"),
      {
        ...EMPTY_DASHBOARD_URL_STATE,
        query: " car ",
        status: "pending_review",
        data_type: ["video", "image", "video"],
        member_id: " u1 ",
        created_from: "2026-01-02",
        created_to: "2026-02-03",
      },
    );
    expect(encoded.get("q")).toBe("car");
    expect(encoded.getAll("data_type")).toEqual(["image", "video"]);
    expect(encoded.get("member_id")).toBe("u1");
    expect(encoded.get("new")).toBe("1");
    expect(encoded.get("from")).toBe("p1");
    expect(encoded.get("layout")).toBe("grid");
    expect(encoded.get("keep")).toBe("1");

    expect(parseDashboardUrlWithIssues(encoded)).toEqual({
      state: {
        ...EMPTY_DASHBOARD_URL_STATE,
        query: "car",
        status: "pending_review",
        data_type: ["image", "video"],
        member_id: "u1",
        created_from: "2026-01-02",
        created_to: "2026-02-03",
      },
      issues: [],
    });
  });

  it("clears only filters and safely drops unknown or reversed values", () => {
    const parsed = parseDashboardUrlWithIssues(
      "q=x&status=unknown&data_type=video&data_type=bogus&created_from=2026-03-01&created_to=2026-02-01&layout=grid",
    );
    expect(parsed.state).toEqual({
      ...EMPTY_DASHBOARD_URL_STATE,
      query: "x",
      data_type: ["video"],
    });
    expect(parsed.issues.map((issue) => issue.key)).toEqual(["status", "data_type", "created_to"]);

    const cleared = dashboardUrlCodec.clear!(
      new URLSearchParams(
        "q=x&status=completed&data_type=image&member_id=u1&created_from=2026-01-01&new=1&layout=grid",
      ),
      EMPTY_DASHBOARD_URL_STATE,
    );
    expect(cleared.toString()).toBe("new=1&layout=grid");
  });
});
