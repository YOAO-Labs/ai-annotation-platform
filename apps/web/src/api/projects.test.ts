import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn((..._args: unknown[]) => Promise.resolve({}));
const binary = vi.hoisted(() => ({ post: vi.fn(), content: vi.fn(), remove: vi.fn() }));

vi.mock("./client", () => ({
  apiClient: {
    get: (...args: unknown[]) => get(...args),
    post: vi.fn(),
    put: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
    silentPost: binary.post,
    silentGetBlob: binary.content,
    silentDelete: binary.remove,
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

describe("guide image byte transport", () => {
  it("sends the original File with its MIME and an authenticated client-owned signal", () => {
    const file = new File(["image"], "中文 %.svg", { type: "image/svg+xml" });
    const signal = new AbortController().signal;
    projectsApi.guideAssets.upload("project", file, { signal });
    expect(binary.post).toHaveBeenCalledWith(
      `/projects/project/guide-assets/upload?file_name=${encodeURIComponent(file.name)}`,
      undefined,
      { body: file, headers: { "Content-Type": "image/svg+xml" }, signal },
    );
    expect(binary.post.mock.calls[0][2].body).toBe(file);
  });

  it("reads and removes an exact encoded key with silent cancellable requests", () => {
    const signal = new AbortController().signal;
    const key = "projects/origin/guide/中文 %25";
    projectsApi.guideAssets.content("copy", key, { signal });
    projectsApi.guideAssets.remove("copy", key, { signal });
    expect(binary.content).toHaveBeenCalledWith(
      `/projects/copy/guide-assets/content?key=${encodeURIComponent(key)}`,
      { signal },
    );
    expect(binary.remove).toHaveBeenCalledWith(
      `/projects/copy/guide-assets?key=${encodeURIComponent(key)}`,
      { signal },
    );
  });
});
