import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DirectoryMoveConfirmation } from "./DirectoryMoveConfirmation";
import type { DatasetSourceChangeOut } from "@/api/generated/types.gen";

const api = vi.hoisted(() => ({ moveCandidates: vi.fn() }));
const image = vi.hoisted(() => ({
  url: "blob:target" as string | undefined,
  error: undefined as string | undefined,
  retry: vi.fn(),
}));
vi.mock("@/api/datasetSources", () => ({ datasetSourcesApi: api }));
vi.mock("@/hooks/useVerifiedImage", () => ({
  useVerifiedImage: () => image,
  verifiedContentError: () => "候选读取失败",
}));
const old: DatasetSourceChangeOut = {
  path_digest: "a",
  relative_path: "old.png",
  kind: "not_seen",
  reason: "not_enumerated",
  before_item_id: "item",
  before_revision_id: "r1",
  before_sha256: "sha",
  candidate_sha256: null,
  accepted_item_id: null,
  accepted_revision_id: null,
  can_accept_revision: false,
  can_revalidate: false,
  can_relocate: true,
  relocated_to_path: null,
  registration_state: null,
  error_code: null,
};
const first = { path_digest: "b", relative_path: "target/a.png", candidate_sha256: "sha" };
const second = { path_digest: "c", relative_path: "target/b.png", candidate_sha256: "sha" };
function mount(confirm = vi.fn()) {
  render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <DirectoryMoveConfirmation
        entry={old}
        registrationId="reg"
        scanId="scan"
        busy={false}
        error=""
        onClose={vi.fn()}
        onConfirm={confirm}
      />
    </QueryClientProvider>,
  );
  return confirm;
}
beforeEach(() => {
  image.url = "blob:target";
  api.moveCandidates.mockResolvedValue({ total: 2, items: [first, second] });
});
describe("显式移动目标确认", () => {
  it("不自动选择相同SHA，选择与勾选后才提交固定目标", async () => {
    const confirm = mount();
    await screen.findByLabelText("target/a.png");
    const button = screen.getByRole("button", { name: "确认移动此样本" });
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByLabelText("target/a.png"));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByLabelText("target/b.png"));
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(button);
    expect(confirm).toHaveBeenCalledWith(second);
  });
  it("候选未读取成功不能确认", async () => {
    image.url = undefined;
    mount();
    fireEvent.click(await screen.findByLabelText("target/a.png"));
    fireEvent.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "确认移动此样本" })).toBeDisabled();
  });
  it("按页加载候选，分页不自动改写选中目标", async () => {
    api.moveCandidates
      .mockResolvedValueOnce({ total: 21, items: [first] })
      .mockResolvedValueOnce({ total: 21, items: [second] });
    mount();
    fireEvent.click(await screen.findByLabelText("target/a.png"));
    fireEvent.click(screen.getByRole("button", { name: "下一页候选" }));
    await screen.findByLabelText("target/b.png");
    await waitFor(() =>
      expect(api.moveCandidates).toHaveBeenCalledWith("reg", "a", 20, expect.any(AbortSignal)),
    );
    expect(screen.getByText("移动到：target/a.png")).toBeInTheDocument();
  });
});
