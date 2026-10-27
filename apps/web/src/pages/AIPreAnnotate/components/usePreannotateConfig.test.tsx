import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { PropsWithChildren } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthStore } from "@/stores/authStore";
import { usePreannotateConfig } from "./usePreannotateConfig";

const mocks = vi.hoisted(() => ({
  project: { data_type: "image", classes_config: {} },
  catalog: {} as Record<string, unknown>,
  setup: vi.fn(),
  capabilities: vi.fn(),
  save: vi.fn(),
}));
vi.mock("@/hooks/useProjects", () => ({
  useProject: () => ({ data: mocks.project }),
  useUpdateProject: () => ({ mutate: vi.fn() }),
}));
vi.mock("@/api/modelCatalog", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/api/modelCatalog")>()),
  useProjectDetectionCatalog: () => mocks.catalog,
}));
vi.mock("@/api/ml-backends", () => ({
  mlBackendSetupQueryKey: (...values: unknown[]) => ["setup", ...values],
  mlBackendsApi: { setup: mocks.setup, capabilities: mocks.capabilities },
}));
vi.mock("@/pages/Workbench/state/useAiToolParamPrefs", () => ({
  useAiToolParamPrefs: () => ({ savedParams: undefined, save: mocks.save }),
}));
const params = { type: "object", properties: { conf: { type: "number", default: 0.25 } } };
function model(id: string, digest: string) {
  return {
    id,
    model_id: id,
    source: "project_trained",
    name: id,
    task: "detect",
    model_variants: {},
    output_classes: ["项目车辆", "项目行人"],
    class_metadata: {
      class_source: "checkpoint",
      verification_status: "verified",
      classes_digest: digest,
    },
    capabilities: {
      id,
      task: "detect",
      supported_inputs: ["full_image", "crop"],
      supported_prompts: ["none"],
      supported_geometric_outputs: ["bbox"],
      classes: [
        { index: 0, name: "car" },
        { index: 1, name: "person" },
      ],
      params,
    },
    placements: [
      { ready: true, registry_id: "qualified-backend", deployment_id: "deployment", params },
    ],
    operations: {
      preannotate: { allowed: true },
      workbench: { allowed: true },
      pipeline: { allowed: true },
    },
  };
}
function useConfig(backendId: string | null = null) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: PropsWithChildren) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return renderHook(
    function useSelectedConfig({ backend }) {
      return usePreannotateConfig({
        projectId: "project",
        backendId: backend,
        executionScope: "workbench",
      });
    },
    { wrapper, initialProps: { backend: backendId } },
  );
}
beforeEach(() => {
  vi.clearAllMocks();
  mocks.project = { data_type: "image", classes_config: {} };
  mocks.catalog = {
    isSuccess: true,
    isLoading: false,
    data: {
      items: [model("trained:first", "digest-a")],
      current_release: { entry_id: "trained:first", deployment_id: "deployment" },
    },
  };
  useAuthStore.setState({ user: { id: "member", role: "employee" } as never });
});
describe("project catalog selection", () => {
  it("keeps the selected model while the legacy backend initializes and avoids admin discovery", () => {
    const { result, rerender } = useConfig();
    act(() => result.current.setCatalogModel("trained:first"));
    rerender({ backend: "legacy-default" });
    expect(result.current.selectedCatalogId).toBe("trained:first");
    expect(result.current.buildArgs("overwrite")).toMatchObject({
      ml_backend_id: "qualified-backend",
      model_id: "trained:first",
      task: "detect",
      model_variants: {},
    });
    expect(mocks.setup).not.toHaveBeenCalled();
    expect(mocks.capabilities).not.toHaveBeenCalled();
    expect(result.current.selectedCatalogEntry?.output_classes).toEqual(["项目车辆", "项目行人"]);
  });
  it("applies presets and native-index filters to the actual request", () => {
    const { result } = useConfig();
    act(() => result.current.setCatalogModel("trained:first"));
    act(() => {
      result.current.applyPreset({ conf: 0.61, unknown: 1 });
      result.current.setSelectedClassIdx(new Set([1]));
    });
    expect(result.current.buildArgs("overwrite")).toMatchObject({
      params: { conf: 0.61 },
      class_filter: [1],
    });
    expect(result.current.buildArgs("overwrite")?.params).not.toHaveProperty("unknown");
  });
  it("clears stale native filters when the current-release alias resolves to a new version", () => {
    const { result, rerender } = useConfig();
    act(() => result.current.setCatalogModel("project-release"));
    act(() => result.current.setSelectedClassIdx(new Set([1])));
    mocks.catalog = {
      ...mocks.catalog,
      data: {
        items: [model("trained:second", "digest-b")],
        current_release: { entry_id: "trained:second", deployment_id: "deployment" },
      },
    };
    rerender({ backend: null });
    expect(result.current.selectedClassIdx.size).toBe(0);
    expect(result.current.buildArgs("overwrite")?.model_id).toBe("project-release");
  });
  it("explains the image-only boundary before a video model can be submitted", () => {
    mocks.project = { data_type: "video", classes_config: {} };
    const { result } = useConfig();
    act(() => result.current.setCatalogModel("trained:first"));
    expect(result.current.catalogReason).toContain("图片项目");
    expect(result.current.configReady).toBe(false);
    expect(result.current.buildArgs("overwrite")).toBeNull();
  });
});
