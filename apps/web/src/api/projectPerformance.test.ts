import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { server } from "@/mocks/server";
import { resetAuthUser, seedAuthUser } from "@/test/auth";
import { projectPerformanceApi } from "./projectPerformance";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("./client", () => ({
  apiClient: { get },
}));

describe("project performance API", () => {
  beforeEach(() => {
    get.mockReset();
  });
  afterEach(resetAuthUser);

  it("encodes the shared scope and sort direction for list requests", async () => {
    const signal = new AbortController().signal;
    get.mockResolvedValue({ items: [] });
    await projectPerformanceApi.getMembers(
      "project/1",
      {
        from: "2026-09-01",
        to: "2026-09-08",
        timezone: "Asia/Shanghai",
        work_type: "review",
        account_status: "inactive",
        include_historical: true,
        q: "Ada & Grace",
        sort: "-review_decisions",
        cursor: "next/1",
        limit: 50,
      },
      signal,
    );
    expect(get).toHaveBeenCalledWith(
      "/projects/project%2F1/performance/members?from=2026-09-01&to=2026-09-08&timezone=Asia%2FShanghai&work_type=review&account_status=inactive&include_historical=true&q=Ada+%26+Grace&sort=-review_decisions&cursor=next%2F1&limit=50",
      { signal },
    );
  });

  it("uses the member event path and supports cancellation", async () => {
    const signal = new AbortController().signal;
    get.mockResolvedValue({ items: [], next_cursor: null });
    await projectPerformanceApi.getMemberEvents(
      "project-1",
      "user-1",
      {
        work_type: "annotation",
        account_status: "all",
        include_historical: false,
        cursor: null,
        limit: 20,
      },
      signal,
    );
    expect(get).toHaveBeenCalledWith(
      "/projects/project-1/performance/members/user-1/events?work_type=annotation&account_status=all&include_historical=false&limit=20",
      { signal },
    );
  });

  it("downloads the filtered CSV with the requested ordering, not just the visible page", async () => {
    seedAuthUser();
    const requests: Request[] = [];
    const csv = "name,review_decisions\nAda & Grace,4\n";
    server.use(
      http.get("*/api/v1/projects/:project/performance/export", ({ request }) => {
        requests.push(request);
        return new HttpResponse(csv, {
          headers: {
            "Content-Type": "text/csv",
            "Content-Disposition": "attachment; filename*=UTF-8''review%20decisions.csv",
          },
        });
      }),
    );
    const result = await projectPerformanceApi.exportMembers("project/1", {
      from: "2026-09-01",
      to: "2026-09-08",
      timezone: "UTC",
      work_type: "review",
      account_status: "all",
      include_historical: false,
      q: "Ada & Grace",
      sort: "+review_decisions",
      cursor: null,
    });
    expect(requests).toHaveLength(1);
    const url = new URL(requests[0].url);
    expect(url.pathname).toBe("/api/v1/projects/project%2F1/performance/export");
    expect([...url.searchParams].sort()).toEqual(
      Object.entries({
        from: "2026-09-01",
        to: "2026-09-08",
        timezone: "UTC",
        work_type: "review",
        account_status: "all",
        include_historical: "false",
        q: "Ada & Grace",
        sort: "+review_decisions",
      }).sort(),
    );
    expect(result.filename).toBe("review decisions.csv");
    expect(await result.blob.text()).toBe(csv);
  });
});
