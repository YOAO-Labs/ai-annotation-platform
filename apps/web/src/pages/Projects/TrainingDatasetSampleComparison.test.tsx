import { useState } from "react";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  trainingDatasetsApi,
  type DatasetSampleFilters,
  type TrainingDatasetAnalyzedSampleOut,
  type TrainingDatasetSamplePage,
} from "@/api/trainingDatasets";
import type { MeResponse } from "@/api/auth";
import { Modal } from "@/components/ui/Modal";
import { useAuthStore } from "@/stores/authStore";
import { insightBox, insightSample } from "@/test/trainingDatasetInsightsFixtures";
import { TrainingDatasetSampleComparison } from "./TrainingDatasetSampleComparison";

vi.mock("@/api/trainingDatasets", () => ({
  trainingDatasetsApi: { samples: vi.fn(), sample: vi.fn(), sampleMedia: vi.fn() },
}));

const CONTENT = "c".repeat(64);
const clients: QueryClient[] = [];
const retained = new Map<string, TrainingDatasetAnalyzedSampleOut>();
const close = vi.fn();
const observed = vi.fn();
const createUrl = vi.fn(() => "blob:comparison");
const revokeUrl = vi.fn();
type Props = Parameters<typeof TrainingDatasetSampleComparison>[0];
const initial: Props = {
  projectId: "project-1",
  baselineVersionId: "baseline",
  targetVersionId: "target",
  contentSha256: CONTENT,
  baselineUnknownContents: 0,
  targetUnknownContents: 0,
  onClose: close,
  onObserved: observed,
};

function member(versionId: string, taskId = `${versionId}-member`, sha256 = CONTENT) {
  return { ...insightSample, version_id: versionId, task_id: taskId, sha256 };
}

function page(
  versionId: string,
  items: TrainingDatasetAnalyzedSampleOut[],
  total = items.length,
  offset = 0,
): TrainingDatasetSamplePage {
  items.forEach((sample) => retained.set(`${sample.version_id}/${sample.task_id}`, sample));
  return { source_version_id: versionId, items, total, offset, limit: 20 };
}

function normalMembers(_projectId: string, versionId: string, filters: DatasetSampleFilters) {
  const hash =
    (filters as DatasetSampleFilters & { content_sha256?: string }).content_sha256 ?? CONTENT;
  return Promise.resolve(
    page(versionId, [member(versionId, `${versionId}-member`, hash)], 1, filters.offset),
  );
}

function client() {
  const value = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  clients.push(value);
  return value;
}

function mount(overrides: Partial<Props> = {}) {
  const queryClient = client();
  const tree = (next: Partial<Props> = overrides) => (
    <QueryClientProvider client={queryClient}>
      <TrainingDatasetSampleComparison {...initial} {...next} />
    </QueryClientProvider>
  );
  return { ...render(tree()), update: tree };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.resetAllMocks();
  retained.clear();
  createUrl.mockImplementation(() => `blob:comparison-${createUrl.mock.calls.length}`);
  vi.stubGlobal(
    "URL",
    Object.assign(globalThis.URL, { createObjectURL: createUrl, revokeObjectURL: revokeUrl }),
  );
  useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
  vi.mocked(trainingDatasetsApi.samples).mockImplementation(normalMembers);
  vi.mocked(trainingDatasetsApi.sample).mockImplementation(
    async (_projectId, versionId, taskId, offset = 0) => ({
      sample: retained.get(`${versionId}/${taskId}`)!,
      labels: [
        {
          ...insightBox,
          type: "bbox",
          class_index: 0,
          class_name: versionId === "baseline" ? "基线类别" : "当前类别",
        },
      ],
      total: 1,
      offset,
      limit: 20,
    }),
  );
  vi.mocked(trainingDatasetsApi.sampleMedia).mockResolvedValue(
    new Blob(["frozen"], { type: "image/png" }),
  );
});

afterEach(() => {
  cleanup();
  clients.splice(0).forEach((value) => value.clear());
  useAuthStore.getState().logout();
  vi.unstubAllGlobals();
});

describe("frozen content comparison", () => {
  it("queries complete content identity and copies the full hash without closing the dialog", async () => {
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue(undefined);
    mount();
    const baseline = screen.getByRole("region", { name: "基线版本" });
    const target = screen.getByRole("region", { name: "当前版本" });
    expect(await within(baseline).findByText("基线类别")).toBeVisible();
    expect(await within(target).findByText("当前类别")).toBeVisible();
    expect(within(baseline).queryByText("当前类别")).not.toBeInTheDocument();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    for (const versionId of ["baseline", "target"]) {
      expect(trainingDatasetsApi.samples).toHaveBeenCalledWith(
        "project-1",
        versionId,
        { limit: 20, offset: 0, content_sha256: CONTENT },
        expect.objectContaining({ signal: expect.any(AbortSignal) }),
      );
      expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledWith(
        "project-1",
        versionId,
        `${versionId}-member`,
        expect.anything(),
      );
    }
    const copyHash = screen.getByRole("button", { name: "复制样本内容 SHA256" });
    expect(copyHash).toBeVisible();
    await user.click(copyHash);
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(CONTENT));
    expect(close).not.toHaveBeenCalled();
  });

  it.each([
    ["baseline", "新增内容：该内容未出现在基线版本中。"],
    ["target", "移除内容：该内容未出现在当前版本中。"],
  ])(
    "shows an absent %s only after the opposite version has actual members",
    async (absent, message) => {
      vi.mocked(trainingDatasetsApi.samples).mockImplementation(async (_projectId, versionId) =>
        page(versionId, versionId === absent ? [] : [member(versionId)]),
      );
      mount();
      expect(await screen.findByText(message)).toBeVisible();
      expect(trainingDatasetsApi.sample).not.toHaveBeenCalledWith(
        "project-1",
        absent,
        expect.anything(),
        expect.anything(),
        expect.anything(),
        20,
      );
    },
  );

  it("keeps failed member evidence unknown and lets a retry establish an added sample", async () => {
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(async (_projectId, versionId) => {
      if (versionId === "target") throw new Error("目标版本分析暂不可用");
      return page(versionId, []);
    });
    mount();
    expect(await screen.findByText("该版本中没有该内容的训练成员；另一侧尚未确认。")).toBeVisible();
    expect(await screen.findByRole("alert")).toHaveTextContent("目标版本分析暂不可用");
    expect(screen.queryByText(/新增内容：|移除内容：/)).not.toBeInTheDocument();
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(normalMembers);
    await userEvent.click(screen.getByRole("button", { name: "重试成员读取" }));
    expect(await screen.findByText("新增内容：该内容未出现在基线版本中。")).toBeVisible();
  });
  it("does not assert an addition when the baseline has members with unknown content identity", async () => {
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(async (_projectId, versionId) =>
      page(versionId, versionId === "baseline" ? [] : [member(versionId)]),
    );
    mount({ baselineUnknownContents: 1 });
    expect(await screen.findByText(/该版本已知身份中未找到匹配；内容身份未知 1 张/)).toBeVisible();
    expect(screen.queryByText(/新增内容：/)).not.toBeInTheDocument();
  });

  it("does not call content absent from both versions an addition and removal", async () => {
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(async (_projectId, versionId) =>
      page(versionId, []),
    );
    mount();
    await waitFor(() =>
      expect(screen.getAllByText("两侧都没有该内容的训练成员。")).toHaveLength(2),
    );
    expect(trainingDatasetsApi.sampleMedia).not.toHaveBeenCalled();
  });

  it("selects duplicate members explicitly and paginates their actual total without changing the opposite member", async () => {
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(
      async (_projectId, versionId, filters) => {
        if (versionId === "target") return page(versionId, [member(versionId)]);
        const offset = filters.offset ?? 0;
        return page(
          versionId,
          Array.from({ length: Math.min(filters.limit ?? 20, 12 - offset) }, (_, index) =>
            member(versionId, `baseline-${index + offset}`),
          ),
          12,
          offset,
        );
      },
    );
    mount();
    const baseline = screen.getByRole("region", { name: "基线版本" });
    expect(await within(baseline).findByText("共 12 个训练成员")).toBeVisible();
    await userEvent.selectOptions(
      within(baseline).getByRole("combobox", { name: "每页基线版本成员数" }),
      "10",
    );
    await userEvent.selectOptions(
      within(baseline).getByLabelText("基线版本冻结成员"),
      "baseline-7",
    );
    await waitFor(() =>
      expect(trainingDatasetsApi.sample).toHaveBeenCalledWith(
        "project-1",
        "baseline",
        "baseline-7",
        0,
        expect.anything(),
        20,
      ),
    );
    await userEvent.click(within(baseline).getByRole("button", { name: "下一页成员" }));
    expect(await within(baseline).findByText("11–12 / 12 个成员")).toBeVisible();
    await waitFor(() =>
      expect(trainingDatasetsApi.sample).toHaveBeenCalledWith(
        "project-1",
        "baseline",
        "baseline-10",
        0,
        expect.anything(),
        20,
      ),
    );
    expect(within(baseline).getByLabelText("基线版本冻结成员")).toHaveValue("baseline-10");
    expect(
      vi.mocked(trainingDatasetsApi.sampleMedia).mock.calls.filter((call) => call[1] === "target"),
    ).toHaveLength(1);
    expect(revokeUrl).toHaveBeenCalled();
  });

  it("aborts old content queries and never paints their late responses after a version/content switch", async () => {
    const oldBaseline = deferred<TrainingDatasetSamplePage>();
    const oldTarget = deferred<TrainingDatasetSamplePage>();
    vi.mocked(trainingDatasetsApi.samples).mockImplementation((_projectId, versionId) =>
      versionId === "baseline" ? oldBaseline.promise : oldTarget.promise,
    );
    const mounted = mount();
    const signals = vi
      .mocked(trainingDatasetsApi.samples)
      .mock.calls.map((call) => call[3]!.signal!);
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(normalMembers);
    mounted.rerender(
      mounted.update({
        projectId: "project-2",
        baselineVersionId: "next-baseline",
        targetVersionId: "next-target",
        contentSha256: "e".repeat(64),
      }),
    );
    await waitFor(() =>
      expect(trainingDatasetsApi.sample).toHaveBeenCalledWith(
        "project-2",
        "next-target",
        "next-target-member",
        0,
        expect.anything(),
        20,
      ),
    );
    await act(async () => {
      oldBaseline.resolve(page("baseline", [member("baseline")]));
      oldTarget.resolve(page("target", [member("target")]));
    });
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(
      vi.mocked(trainingDatasetsApi.sample).mock.calls.every((call) => call[0] === "project-2"),
    ).toBe(true);
  });

  it("rejects a changed shared credential before a late member response can start preview requests", async () => {
    const baseline = deferred<TrainingDatasetSamplePage>();
    const target = deferred<TrainingDatasetSamplePage>();
    vi.mocked(trainingDatasetsApi.samples).mockImplementation((_projectId, versionId) =>
      versionId === "baseline" ? baseline.promise : target.promise,
    );
    mount();
    localStorage.setItem("token", "other-session");
    await act(async () => {
      baseline.resolve(page("baseline", [member("baseline")]));
      target.resolve(page("target", [member("target")]));
    });
    expect(trainingDatasetsApi.sample).not.toHaveBeenCalled();
    expect(trainingDatasetsApi.sampleMedia).not.toHaveBeenCalled();
    expect(createUrl).not.toHaveBeenCalled();
  });

  it("retires old member queries after batched A to B to A session changes", async () => {
    const retiredBaseline = deferred<TrainingDatasetSamplePage>();
    const retiredTarget = deferred<TrainingDatasetSamplePage>();
    vi.mocked(trainingDatasetsApi.samples)
      .mockImplementationOnce(() => retiredBaseline.promise)
      .mockImplementationOnce(() => retiredTarget.promise);
    mount();
    const signals = vi
      .mocked(trainingDatasetsApi.samples)
      .mock.calls.map((call) => call[3]!.signal!);
    act(() => {
      useAuthStore.getState().setAuth("session-b", { id: "user-b" } as MeResponse);
      useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
    });
    expect(await screen.findByText("当前类别")).toBeVisible();
    await waitFor(() => expect(observed).toHaveBeenCalledTimes(2));
    expect(trainingDatasetsApi.samples).toHaveBeenCalledTimes(4);
    await act(async () => {
      retiredBaseline.resolve(page("baseline", [member("baseline", "retired-baseline-member")]));
      retiredTarget.resolve(page("target", [member("target", "retired-target-member")]));
    });
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(
      vi
        .mocked(trainingDatasetsApi.sample)
        .mock.calls.some((call) => call[2].startsWith("retired-")),
    ).toBe(false);
    expect(createUrl).toHaveBeenCalledTimes(2);
    expect(observed).toHaveBeenCalledTimes(2);
  });

  it("retires both old media requests after batched A to B to A session changes", async () => {
    const retiredBaseline = deferred<Blob>();
    const retiredTarget = deferred<Blob>();
    vi.mocked(trainingDatasetsApi.sampleMedia)
      .mockImplementationOnce(() => retiredBaseline.promise)
      .mockImplementationOnce(() => retiredTarget.promise);
    mount();
    await waitFor(() => expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledTimes(2));
    const signals = vi
      .mocked(trainingDatasetsApi.sampleMedia)
      .mock.calls.map((call) => call[3]!.signal!);
    act(() => {
      useAuthStore.getState().setAuth("session-b", { id: "user-b" } as MeResponse);
      useAuthStore.getState().setAuth("session-a", { id: "user-a" } as MeResponse);
    });
    await waitFor(() => expect(observed).toHaveBeenCalledTimes(2));
    expect(trainingDatasetsApi.sampleMedia).toHaveBeenCalledTimes(4);
    await act(async () => {
      retiredBaseline.resolve(new Blob(["retired baseline"]));
      retiredTarget.resolve(new Blob(["retired target"]));
    });
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(createUrl).toHaveBeenCalledTimes(2);
    expect(observed).toHaveBeenCalledTimes(2);
  });

  it("aborts pending members on close and ignores late results", async () => {
    const pending = deferred<TrainingDatasetSamplePage>();
    vi.mocked(trainingDatasetsApi.samples).mockReturnValue(pending.promise);
    const mounted = mount();
    const signals = vi
      .mocked(trainingDatasetsApi.samples)
      .mock.calls.map((call) => call[3]!.signal!);
    mounted.unmount();
    await act(async () => pending.resolve(page("baseline", [member("baseline")])));
    expect(signals.every((signal) => signal.aborted)).toBe(true);
    expect(trainingDatasetsApi.sampleMedia).not.toHaveBeenCalled();
  });

  it("rejects a mismatched member response instead of reading another version's media", async () => {
    vi.mocked(trainingDatasetsApi.samples).mockImplementation(async (_projectId, versionId) =>
      page(versionId, [member(versionId, `${versionId}-member`, "f".repeat(64))]),
    );
    mount();
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getAllByRole("alert")[0]).toHaveTextContent(
      "冻结成员响应与所选版本或内容身份不一致",
    );
    expect(trainingDatasetsApi.sample).not.toHaveBeenCalled();
  });

  it("rejects truncated content hashes before making any request", async () => {
    mount({ contentSha256: CONTENT.slice(0, 8) });
    expect(screen.getByRole("alert")).toHaveTextContent("完整内容 SHA 不可用");
    expect(trainingDatasetsApi.samples).not.toHaveBeenCalled();
  });

  it("scopes Escape to the comparison and restores focus to its opener inside an owning dialog", async () => {
    const outerClose = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <Modal open title="外层项目" onClose={outerClose}>
          <button onClick={() => setOpen(true)}>打开样本对照</button>
          {open && <TrainingDatasetSampleComparison {...initial} onClose={() => setOpen(false)} />}
        </Modal>
      );
    }
    render(
      <QueryClientProvider client={client()}>
        <Harness />
      </QueryClientProvider>,
    );
    const opener = screen.getByRole("button", { name: "打开样本对照" });
    await userEvent.click(opener);
    expect(await screen.findByRole("dialog", { name: "冻结样本前后对照" })).toBeVisible();
    await userEvent.keyboard("{Escape}");
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "冻结样本前后对照" })).not.toBeInTheDocument(),
    );
    expect(outerClose).not.toHaveBeenCalled();
    await waitFor(() => expect(opener).toHaveFocus());
  });
  it("changes one side's member page size without changing the opposite side", async () => {
    const user = userEvent.setup();
    mount();
    const baseline = screen.getByRole("region", { name: "基线版本" });
    await within(baseline).findByText("基线类别");
    await user.selectOptions(
      within(baseline).getByRole("combobox", { name: "每页基线版本成员数" }),
      "50",
    );
    await waitFor(() =>
      expect(trainingDatasetsApi.samples).toHaveBeenLastCalledWith(
        "project-1",
        "baseline",
        { limit: 50, offset: 0, content_sha256: CONTENT },
        expect.anything(),
      ),
    );
    expect(
      vi.mocked(trainingDatasetsApi.samples).mock.calls.filter((call) => call[1] === "target"),
    ).toHaveLength(1);
  });
});
