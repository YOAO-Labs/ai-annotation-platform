import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { DatasetSourceOut } from "@/api/generated/types.gen";
import { DirectorySourceRules } from "./DirectorySourceRules";
const api = vi.hoisted(() => ({ previewRules: vi.fn(), applyRules: vi.fn() }));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useSessionGuard", () => ({ useSessionGuard: () => () => () => true }));
const source: DatasetSourceOut = {
  id: "source",
  cache_policy: "none",
  effective_cache_policy: "none",
  location_cache_policy: "preview",
  dataset_id: "dataset",
  enabled: true,
  location_id: "location",
  name: "source",
  node_id: "node",
  ownership_mode: "reference",
  policy_revision: 1,
  relative_path: "root",
  config_revision: 2,
  recursive: true,
  include_globs: ["*"],
  exclude_globs: [],
  unregistered_at: null,
};
beforeEach(() => {
  api.previewRules.mockResolvedValue({
    proof: "proof",
    total: 1,
    counts: { leaving_scope: 1 },
    active_attempt_count: 0,
    items: [{ path_digest: "digest", relative_path: "a.png", impact: "leaving_scope" }],
  });
  api.applyRules.mockResolvedValue({});
});
const act = async (work: (current: () => boolean) => Promise<unknown>) => {
  await work(() => true);
};
it("requires preview and explicit review, and drops stale preview after rule changes", async () => {
  render(<DirectorySourceRules source={source} act={act} />);
  expect(screen.queryByRole("button", { name: "确认保存扫描规则" })).not.toBeInTheDocument();
  fireEvent.change(screen.getByRole("textbox", { name: "新的排除规则 1" }), {
    target: { value: "*.png" },
  });
  fireEvent.click(screen.getByRole("button", { name: "预览规则影响" }));
  const confirm = await screen.findByRole("button", { name: "确认保存扫描规则" });
  expect(confirm).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: /已核对影响/ }));
  fireEvent.click(confirm);
  await waitFor(() =>
    expect(api.applyRules).toHaveBeenCalledWith("source", {
      expected_revision: 2,
      recursive: true,
      include_globs: ["*"],
      exclude_globs: ["*.png"],
      impact_proof: "proof",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "确认保存扫描规则" })).not.toBeInTheDocument(),
  );
  fireEvent.click(screen.getByRole("button", { name: "预览规则影响" }));
  await screen.findByRole("button", { name: "确认保存扫描规则" });
  fireEvent.change(screen.getByRole("textbox", { name: "新的排除规则 1" }), {
    target: { value: "other" },
  });
  expect(screen.queryByRole("region", { name: "扫描规则影响预览" })).not.toBeInTheDocument();
});
it("keeps active-attempt previews read only", async () => {
  api.previewRules.mockResolvedValue({
    proof: "proof",
    total: 0,
    counts: {},
    active_attempt_count: 1,
    items: [],
  });
  render(<DirectorySourceRules source={source} act={act} />);
  fireEvent.click(screen.getByRole("button", { name: "预览规则影响" }));
  await screen.findByText(/请先结束或取消活动扫描/);
  fireEvent.click(screen.getByRole("checkbox", { name: /已核对影响/ }));
  expect(screen.getByRole("button", { name: "确认保存扫描规则" })).toBeDisabled();
});

it("roundtrips exact glob arrays including commas and significant spaces", async () => {
  const globs = ["[a,b].png", " leading and trailing .png ", "line\nbreak.png"];
  render(
    <DirectorySourceRules
      source={{ ...source, include_globs: globs, exclude_globs: ["[x,y].png"] }}
      act={act}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "预览规则影响" }));
  await screen.findByRole("region", { name: "扫描规则影响预览" });
  expect(api.previewRules).toHaveBeenCalledWith(
    "source",
    { expected_revision: 2, recursive: true, include_globs: globs, exclude_globs: ["[x,y].png"] },
    0,
  );
});
