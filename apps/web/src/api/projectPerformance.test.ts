import { beforeEach, describe, expect, it, vi } from "vitest";
import { projectPerformanceApi, projectPerformanceQueryString } from "./projectPerformance";

const { get } = vi.hoisted(() => ({ get: vi.fn() }));

vi.mock("./client", () => ({
  apiClient: { get },
}));

const authState = {
  token: "token-1",
  user: { id: "owner-1" },
};

vi.mock("@/stores/authStore", () => ({
  useAuthStore: { getState: () => authState },
  isCurrentAuthOwner: () => true,
}));

describe("project performance API", () => {
  beforeEach(() => {
    get.mockReset();
    global.fetch = vi.fn();
  });

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

  it("exposes query encoding independently for CSV parity", () => {
    expect(
      projectPerformanceQueryString({
        from: "2026-09-01",
        to: "2026-09-08",
        timezone: "UTC",
        work_type: "annotation",
        account_status: "all",
        include_historical: false,
        sort: "+name",
      }),
    ).toBe(
      "from=2026-09-01&to=2026-09-08&timezone=UTC&work_type=annotation&account_status=all&include_historical=false&sort=%2Bname",
    );
  });
});
