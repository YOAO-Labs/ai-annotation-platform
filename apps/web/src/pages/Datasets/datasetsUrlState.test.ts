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
      state: { query: "", page: 0, pageSize: 20, data_type: undefined, location_id: undefined },
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
});
