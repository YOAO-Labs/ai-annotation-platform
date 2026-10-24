import { describe, expect, it } from "vitest";
import {
  datasetDetailPath,
  datasetItemFilters,
  datasetFilterPatch,
  datasetFilterSignature,
  datasetsUrlCodec,
  legacyDatasetDetailPath,
  parseDatasetDetailUrl,
  parseDatasetsUrlWithIssues,
  safeDatasetsReturn,
} from "./datasetsUrlState";
describe("Datasets URL state", () => {
  it("round-trips combined sample conditions without silently dropping invalid ranges", () => {
    const filters = datasetItemFilters(
      new URLSearchParams("q=car&extension=JPEG&min_width=640&max_height=1080&media_type=image"),
    );
    expect(filters).toEqual({
      search: "car",
      extension: "JPEG",
      min_width: 640,
      max_height: 1080,
      media_type: "image",
    });
    const patch = datasetFilterPatch(filters);
    expect(patch.q).toBe("car");
    expect(patch.min_width).toBe("640");
    expect(patch.availability).toBeUndefined();
    expect(datasetFilterSignature(filters)).toBe(
      datasetFilterSignature({ ...filters, extension: ".jpg" }),
    );
    expect(datasetItemFilters(new URLSearchParams("min_width=-1&max_width=oops")).min_width).toBe(
      -1,
    );
    expect(
      datasetItemFilters(new URLSearchParams("min_width=-1&max_width=oops")).max_width,
    ).toBeNaN();
  });

  it("restores filters, server pagination and location while preserving unrelated links", () => {
    const parsed = parseDatasetsUrlWithIssues(
      "q= lidar &data_type=point_cloud&page=3&page_size=50&location_id=loc",
    );
    expect(parsed.state).toEqual({
      query: "lidar",
      state: "active",
      data_type: "point_cloud",
      page: 2,
      pageSize: 50,
      location_id: "loc",
    });
    const encoded = datasetsUrlCodec.encode(new URLSearchParams("keep=1&dataset=ds"), parsed.state);
    expect(encoded.get("page")).toBe("3");
    expect(encoded.get("keep")).toBe("1");
    expect(encoded.get("dataset")).toBe("ds");
  });
  it("bounds invalid paging and reports invalid types", () => {
    expect(parseDatasetsUrlWithIssues("page=-2&page_size=999&data_type=unknown")).toEqual({
      state: {
        query: "",
        state: "active",
        page: 0,
        pageSize: 20,
        data_type: undefined,
        location_id: undefined,
      },
      issues: [{ key: "data_type", message: "未知的数据集类型" }],
    });
  });
  it("converts legacy links directly and retains list return context", () => {
    const url = legacyDatasetDetailPath(
      new URLSearchParams("dataset=ds&source=src&scan=run&q=abc&page=4"),
    );
    expect(url).not.toBeNull();
    const query = new URLSearchParams(url!.split("?")[1]);
    expect(query.get("return")).toBe("/datasets?q=abc&page=4");
    expect(query.get("source")).toBe("src");
    expect(query.get("scan")).toBe("run");
    expect(query.get("tab")).toBe("sources");
  });
  it("restores detail filter, grid, inspector and page independently", () => {
    expect(
      parseDatasetDetailUrl(
        new URLSearchParams(
          "tab=data&page=2&page_size=10&view=grid&item=outside&source_id=src&availability=preview_only&q=cat",
        ),
      ),
    ).toMatchObject({
      tab: "data",
      page: 1,
      pageSize: 10,
      view: "grid",
      itemId: "outside",
      sourceId: "src",
      availability: "preview_only",
      query: "cat",
    });
    expect(parseDatasetDetailUrl(new URLSearchParams("source=src&scan=run")).tab).toBe("sources");
  });
  it("accepts only local list or storage return destinations", () => {
    [
      "https://evil.test",
      "//evil.test",
      "/datasets/../settings",
      "/datasets\\evil",
      "/datasets#evil",
      "/storage/elsewhere",
    ].forEach((value) => expect(safeDatasetsReturn(value)).toBe("/datasets"));
    expect(safeDatasetsReturn("/storage?location=loc")).toBe("/storage?location=loc");
    expect(datasetDetailPath("ds", "?q=abc&page=2")).toContain(
      "return=%2Fdatasets%3Fq%3Dabc%26page%3D2",
    );
  });
  it("preserves explicit project statistics selections, including invalid empty values", () => {
    expect(
      parseDatasetDetailUrl(new URLSearchParams("tab=projects")).statsProjectId,
    ).toBeUndefined();
    expect(
      parseDatasetDetailUrl(new URLSearchParams("tab=projects&stats_project=p-two&project_page=2")),
    ).toMatchObject({ tab: "projects", statsProjectId: "p-two", projectPage: 1 });
    expect(parseDatasetDetailUrl(new URLSearchParams("stats_project=")).statsProjectId).toBe("");
    expect(parseDatasetDetailUrl(new URLSearchParams("stats_project=unknown")).statsProjectId).toBe(
      "unknown",
    );
  });
});
it("recognizes the quality tab without consuming item filter context", () => {
  const state = parseDatasetDetailUrl(
    new URLSearchParams("tab=quality&quality_run=run&item=item&q=car&page=3"),
  );
  expect(state.tab).toBe("quality");
  expect(state.itemId).toBe("item");
  expect(state.query).toBe("car");
  expect(state.page).toBe(2);
});

it("round-trips Scene identity and keeps invalid Scene URL conditions for server validation", () => {
  const params = new URLSearchParams("scene_id=scene-a&extension=png&saved_view=view&view_query=1");
  const state = parseDatasetDetailUrl(params);
  expect(state.filters.scene_id).toBe("scene-a");
  expect(datasetFilterPatch(state.filters).scene_id).toBe("scene-a");
  expect(datasetItemFilters(new URLSearchParams("scene_id=invalid")).scene_id).toBe("invalid");
  expect(datasetFilterSignature({ scene_id: "scene-b" })).not.toBe(
    datasetFilterSignature({ scene_id: "scene-a" }),
  );
});

it("preserves archive filters through pagination, legacy links and return navigation", () => {
  const parsed = parseDatasetsUrlWithIssues("state=archived&page=3&q=car");
  expect(parsed.state.state).toBe("archived");
  const encoded = datasetsUrlCodec.encode(new URLSearchParams("keep=1"), parsed.state);
  expect(encoded.get("state")).toBe("archived");
  expect(encoded.get("page")).toBe("3");
  expect(datasetDetailPath("ds", `?${encoded}`)).toContain("state%3Darchived");
  expect(legacyDatasetDetailPath(new URLSearchParams("dataset=ds&state=archived"))).toContain(
    "state%3Darchived",
  );
  expect(parseDatasetsUrlWithIssues("state=invalid")).toMatchObject({
    state: { state: "active" },
    issues: [{ key: "state" }],
  });
});
