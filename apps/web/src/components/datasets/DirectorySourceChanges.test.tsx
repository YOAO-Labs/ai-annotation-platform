import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { DatasetSourceChangeList, DatasetSourceChangeOut } from "@/api/generated/types.gen";
import { DirectorySourceChanges } from "./DirectorySourceChanges";

const image = vi.hoisted(() => ({
  url: "blob:candidate" as string | undefined,
  error: undefined as string | undefined,
  retry: vi.fn(),
}));
vi.mock("@/hooks/useVerifiedImage", () => ({
  useVerifiedImage: () => image,
  verifiedContentError: () => "读取失败",
}));
const entry: DatasetSourceChangeOut = {
  path_digest: "a".repeat(64),
  relative_path: "vehicle.png",
  kind: "changed",
  reason: "content_changed",
  before_item_id: "old-item",
  before_revision_id: "old-revision",
  before_sha256: "b".repeat(64),
  candidate_sha256: "c".repeat(64),
  accepted_revision_id: null,
  accepted_item_id: null,
  can_accept_revision: true,
  registration_state: "error",
  error_code: "reference_content_changed",
};
const data: DatasetSourceChangeList = {
  complete: true,
  observed_at: "2026-10-23T00:00:00Z",
  total: 1,
  counts: { changed: 1 },
  items: [entry],
};
const props = {
  complete: true,
  data,
  pending: false,
  error: null,
  kind: "",
  offset: 0,
  onKind: vi.fn(),
  onOffset: vi.fn(),
  scanId: "scan",
};

beforeEach(() => {
  image.url = "blob:candidate";
  image.error = undefined;
});
describe("逐项内容修订确认", () => {
  it("固定弹窗候选，失败保留确认，成功关闭", async () => {
    const accept = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const { rerender } = render(<DirectorySourceChanges {...props} onAccept={accept} />);
    fireEvent.click(screen.getByRole("button", { name: "接受为新修订" }));
    const confirm = screen.getByRole("button", { name: "确认接纳此项新修订" });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /已核对这一项/ }));
    rerender(
      <DirectorySourceChanges
        {...props}
        data={{ ...data, items: [{ ...entry, candidate_sha256: "d".repeat(64) }] }}
        onAccept={accept}
        actionError="当前修订已变化"
      />,
    );
    fireEvent.click(confirm);
    await waitFor(() => expect(accept).toHaveBeenCalledWith(entry));
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("当前修订已变化");
    fireEvent.click(confirm);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });
  it("未解码或读取失败的候选不能确认", () => {
    image.url = undefined;
    image.error = "来源已变化";
    render(<DirectorySourceChanges {...props} onAccept={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "接受为新修订" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /已核对这一项/ }));
    expect(screen.getByRole("button", { name: "确认接纳此项新修订" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("来源已变化");
  });
  it("接纳结果优先于冻结的旧错误，同摘要身份变化不提供接纳", () => {
    render(
      <DirectorySourceChanges
        {...props}
        data={{
          ...data,
          total: 2,
          items: [
            {
              ...entry,
              accepted_revision_id: "new-revision",
              accepted_item_id: "new-item",
              can_accept_revision: false,
            },
            {
              ...entry,
              path_digest: "e".repeat(64),
              reason: "observation_changed",
              can_accept_revision: false,
            },
          ],
        }}
        onAccept={vi.fn()}
      />,
    );
    expect(screen.getByText(/已接受为新修订 · 新样本 new-item/)).toBeInTheDocument();
    expect(screen.getByText(/身份观测已变化/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "接受为新修订" })).not.toBeInTheDocument();
  });
});
