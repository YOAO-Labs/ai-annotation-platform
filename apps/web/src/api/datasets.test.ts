import { beforeEach, describe, expect, it, vi } from "vitest";

const get = vi.fn((..._a: unknown[]) => Promise.resolve({}));
const post = vi.fn((..._a: unknown[]) => Promise.resolve({}));
const put = vi.fn((..._a: unknown[]) => Promise.resolve({}));
const del = vi.fn((..._a: unknown[]) => Promise.resolve({}));

vi.mock("./client", () => ({
  apiClient: {
    get: (...a: unknown[]) => get(...a),
    post: (...a: unknown[]) => post(...a),
    put: (...a: unknown[]) => put(...a),
    delete: (...a: unknown[]) => del(...a),
  },
}));

import { datasetsApi } from "./datasets";

beforeEach(() => {
  get.mockClear();
  post.mockClear();
  put.mockClear();
  del.mockClear();
});

describe("datasetsApi · endpoint 契约", () => {
  it("list 拼 query string（含 search 编码）", () => {
    datasetsApi.list({
      search: "a b",
      data_type: "lidar",
      has_scenes: true,
      limit: 10,
      offset: 20,
    });
    expect(get).toHaveBeenCalledWith(
      "/datasets?search=a+b&data_type=lidar&has_scenes=true&limit=10&offset=20",
    );
  });

  it("list 无参 → 裸路径", () => {
    datasetsApi.list();
    expect(get).toHaveBeenCalledWith("/datasets");
  });

  it("list forwards AbortSignal without changing the query path", () => {
    const signal = new AbortController().signal;
    datasetsApi.list({ search: "car" }, { signal });
    expect(get).toHaveBeenCalledWith("/datasets?search=car", { signal });
  });

  it("get / create / update", () => {
    datasetsApi.get("d1");
    expect(get).toHaveBeenCalledWith("/datasets/d1");
    const createKey = "35d9bbf7-2410-4d34-9ca0-6cb9b13ea83b";
    datasetsApi.create(
      { name: "n", data_type: "lidar", axis_convention: "apollo" },
      { idempotencyKey: createKey },
    );
    expect(post).toHaveBeenCalledWith(
      "/datasets",
      {
        name: "n",
        data_type: "lidar",
        axis_convention: "apollo",
      },
      { headers: { "Idempotency-Key": createKey } },
    );
    const updateKey = "7b14b573-3203-4d64-8b51-6ab7b5fbe4cb";
    datasetsApi.update("d1", { axis_convention: null }, { idempotencyKey: updateKey });
    expect(put).toHaveBeenCalledWith(
      "/datasets/d1",
      { axis_convention: null },
      {
        headers: { "Idempotency-Key": updateKey },
      },
    );
  });

  it("dataset create/update generate UUID keys when omitted", () => {
    datasetsApi.create({ name: "n" });
    datasetsApi.update("d1", { description: "changed" });
    expect(post).toHaveBeenCalledWith(
      "/datasets",
      { name: "n" },
      {
        headers: { "Idempotency-Key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
      },
    );
    expect(put).toHaveBeenCalledWith(
      "/datasets/d1",
      { description: "changed" },
      {
        headers: { "Idempotency-Key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
      },
    );
  });

  it("sniffAxisConvention POST 到 sniff 端点", () => {
    datasetsApi.sniffAxisConvention("d1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/sniff-axis-convention");
  });

  it("delete / listItems（带/不带分页）", () => {
    const idempotencyKey = "35d9bbf7-2410-4d34-9ca0-6cb9b13ea83b";
    datasetsApi.delete("d1", { idempotencyKey });
    expect(del).toHaveBeenCalledWith("/datasets/d1", undefined, {
      headers: { "Idempotency-Key": idempotencyKey },
    });
    datasetsApi.listItems("d1");
    expect(get).toHaveBeenCalledWith("/datasets/d1/items");
    datasetsApi.listItems("d1", { limit: 5, offset: 10 });
    expect(get).toHaveBeenCalledWith("/datasets/d1/items?limit=5&offset=10");
  });

  it("source deletes generate UUID Idempotency-Key headers when omitted", () => {
    datasetsApi.delete("d1");
    datasetsApi.deleteItem("d1", "i1");
    expect(del).toHaveBeenNthCalledWith(1, "/datasets/d1", undefined, {
      headers: { "Idempotency-Key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
    });
    expect(del).toHaveBeenNthCalledWith(2, "/datasets/d1/items/i1", undefined, {
      headers: { "Idempotency-Key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
    });
  });

  it("upload phases preserve explicit keys and generate UUIDs when omitted", () => {
    const initKey = crypto.randomUUID();
    const completeKey = crypto.randomUUID();
    datasetsApi.uploadInit(
      "d1",
      { file_name: "a.pcd", content_type: "x" },
      { idempotencyKey: initKey },
    );
    expect(post).toHaveBeenCalledWith(
      "/datasets/d1/items/upload-init",
      {
        file_name: "a.pcd",
        content_type: "x",
      },
      { headers: { "Idempotency-Key": initKey } },
    );
    datasetsApi.uploadComplete("d1", "i1", { idempotencyKey: completeKey });
    expect(post).toHaveBeenCalledWith("/datasets/d1/items/upload-complete/i1", undefined, {
      headers: { "Idempotency-Key": completeKey },
    });
    datasetsApi.uploadInit("d1", { file_name: "a.pcd" });
    datasetsApi.uploadComplete("d1", "i1");
    for (const call of post.mock.calls.slice(-2)) {
      expect(call[2]).toEqual({
        headers: { "Idempotency-Key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
      });
    }
  });

  it("scan / backfill", () => {
    datasetsApi.scanItems("d1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/items/scan");
    datasetsApi.backfillDimensions("d1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/backfill-dimensions?batch=50");
    datasetsApi.backfillDimensions("d1", 200);
    expect(post).toHaveBeenCalledWith("/datasets/d1/backfill-dimensions?batch=200");
    datasetsApi.backfillMedia("d1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/backfill-media");
  });

  it("deleteItem / link / unlink / preview-unlink", () => {
    const idempotencyKey = "7b14b573-3203-4d64-8b51-6ab7b5fbe4cb";
    datasetsApi.deleteItem("d1", "i1", { idempotencyKey });
    expect(del).toHaveBeenCalledWith("/datasets/d1/items/i1", undefined, {
      headers: { "Idempotency-Key": idempotencyKey },
    });
    datasetsApi.linkProject("d1", "p1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/link", { project_id: "p1" });
    datasetsApi.unlinkProject("d1", "p1");
    expect(del).toHaveBeenCalledWith("/datasets/d1/link/p1", undefined, {
      headers: { "Idempotency-Key": expect.stringMatching(/^[0-9a-f-]{36}$/) },
    });
    datasetsApi.previewUnlink("d1", "p1");
    expect(get).toHaveBeenCalledWith("/datasets/d1/link/p1/preview-unlink");
  });

  it("getLinkedProjects / listForProject", () => {
    datasetsApi.getLinkedProjects("d1");
    expect(get).toHaveBeenCalledWith("/datasets/d1/projects");
    datasetsApi.listForProject("p1");
    expect(get).toHaveBeenCalledWith("/projects/p1/datasets");
  });
});
