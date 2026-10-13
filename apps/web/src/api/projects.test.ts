import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn((..._args: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}));

import { projectsApi } from "./projects";

beforeEach(() => get.mockClear());

describe("projectsApi.list", () => {
  it("requests a server page with filters and cancellation", () => {
    const signal = new AbortController().signal;
    projectsApi.page(
      { page: 3, page_size: 20, data_type: ["image", "video"], search: "car" },
      { signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/projects/query?page=3&page_size=20&data_type=image&data_type=video&search=car",
      { signal },
    );
  });
  it("serializes repeated project data types and forwards AbortSignal", () => {
    const signal = new AbortController().signal;
    projectsApi.list(
      {
        status: "in_progress",
        search: "car",
        data_type: ["image", "video"],
        member_id: "u1",
      },
      { signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/projects?status=in_progress&search=car&data_type=image&data_type=video&member_id=u1",
      { signal },
    );
  });

  it("keeps the existing bare-path call when no init is supplied", () => {
    projectsApi.list();
    expect(get).toHaveBeenCalledWith("/projects");
  });
});
