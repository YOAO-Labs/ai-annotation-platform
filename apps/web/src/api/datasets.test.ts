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
import { tasksApi } from "./tasks";

beforeEach(() => {
  get.mockClear();
  post.mockClear();
  put.mockClear();
  del.mockClear();
});

describe("datasetsApi · endpoint 契约", () => {
  it("sends bounded format and dimension predicates and personal view paths", () => {
    datasetsApi.listItems("ds", {
      media_type: "image",
      extension: "jpg",
      dimension_state: "known",
      min_width: 640,
      max_height: 1080,
    });
    expect(get).toHaveBeenCalledWith(
      "/datasets/ds/items?media_type=image&extension=jpg&dimension_state=known&min_width=640&max_height=1080",
    );
    const payload = { name: "wide", query: { min_width: 640 } };
    datasetsApi.saveView("ds", payload);
    expect(post).toHaveBeenCalledWith("/datasets/ds/saved-views", payload);
    datasetsApi.saveView("ds", payload, "v");
    expect(put).toHaveBeenCalledWith("/datasets/ds/saved-views/v", payload);
    datasetsApi.deleteView("ds", "v");
    expect(del).toHaveBeenCalledWith("/datasets/ds/saved-views/v");
  });

  it("sends Scene identity in item filters and bounded option selection independently", () => {
    const signal = new AbortController().signal;
    datasetsApi.listItems(
      "ds",
      { scene_id: "scene-a", source_id: "src", extension: "png" },
      { signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/datasets/ds/items?source_id=src&scene_id=scene-a&extension=png",
      { signal },
    );
    datasetsApi.sceneOptions(
      "ds",
      { limit: 20, offset: 20, search: "街道 A", selected_id: "scene-a" },
      { signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/datasets/ds/scene-options?limit=20&offset=20&search=%E8%A1%97%E9%81%93+A&selected_id=scene-a",
      { signal },
    );
  });

  it("limits paged training task choices to the selected dataset on the server", () => {
    const signal = new AbortController().signal;
    tasksApi.listByProject(
      "project",
      { dataset_id: "ds", batch_id: "batch", limit: 25, offset: 25 },
      { signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/tasks?project_id=project&dataset_id=ds&batch_id=batch&limit=25&offset=25",
      { signal },
    );
  });
  it("sends workbench filters and item ownership paths with cancellation", () => {
    const signal = new AbortController().signal;
    datasetsApi.list({ location_id: "loc", offset: 20 });
    expect(get).toHaveBeenCalledWith("/datasets?location_id=loc&offset=20");
    datasetsApi.listItems(
      "ds",
      { limit: 20, offset: 40, search: "a b", source_id: "src", availability: "changed" },
      { signal },
    );
    expect(get).toHaveBeenCalledWith(
      "/datasets/ds/items?limit=20&offset=40&search=a+b&source_id=src&availability=changed",
      { signal },
    );
    datasetsApi.item("ds", "outside", { signal });
    expect(get).toHaveBeenCalledWith("/datasets/ds/items/outside", { signal });
    datasetsApi.workbench("ds", { signal });
    expect(get).toHaveBeenCalledWith("/datasets/ds/workbench", { signal });
    datasetsApi.trainingVersions("ds", { limit: 10, offset: 20 }, { signal });
    expect(get).toHaveBeenCalledWith("/datasets/ds/training-versions?limit=10&offset=20", {
      signal,
    });
  });
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
    datasetsApi.create({ name: "n", data_type: "lidar", axis_convention: "apollo" });
    expect(post).toHaveBeenCalledWith("/datasets", {
      name: "n",
      data_type: "lidar",
      axis_convention: "apollo",
    });
    datasetsApi.update("d1", { axis_convention: null });
    expect(put).toHaveBeenCalledWith("/datasets/d1", { axis_convention: null });
  });

  it("sniffAxisConvention POST 到 sniff 端点", () => {
    datasetsApi.sniffAxisConvention("d1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/sniff-axis-convention");
  });

  it("delete / listItems（带/不带分页）", () => {
    datasetsApi.delete("d1");
    expect(del).toHaveBeenCalledWith("/datasets/d1");
    datasetsApi.listItems("d1");
    expect(get).toHaveBeenCalledWith("/datasets/d1/items");
    datasetsApi.listItems("d1", { limit: 5, offset: 10 });
    expect(get).toHaveBeenCalledWith("/datasets/d1/items?limit=5&offset=10");
  });

  it("upload init / complete", () => {
    datasetsApi.uploadInit("d1", { file_name: "a.pcd", content_type: "x" });
    expect(post).toHaveBeenCalledWith("/datasets/d1/items/upload-init", {
      file_name: "a.pcd",
      content_type: "x",
    });
    datasetsApi.uploadComplete("d1", "i1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/items/upload-complete/i1");
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
    datasetsApi.deleteItem("d1", "i1");
    expect(del).toHaveBeenCalledWith("/datasets/d1/items/i1");
    datasetsApi.linkProject("d1", "p1");
    expect(post).toHaveBeenCalledWith("/datasets/d1/link", { project_id: "p1" });
    datasetsApi.unlinkProject("d1", "p1");
    expect(del).toHaveBeenCalledWith("/datasets/d1/link/p1");
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
