import { describe, expect, it, vi, beforeEach } from "vitest";
import { toast } from "sonner";
import { copyToClipboard } from "@/lib/clipboard";
import { useToastStore } from "./Toast";

vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), { success: vi.fn(), warning: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/lib/clipboard", () => ({ copyToClipboard: vi.fn() }));

beforeEach(() => vi.clearAllMocks());

describe("diagnostic toast copy", () => {
  it.each(["error", "warning"] as const)(
    "copies the complete %s title and details",
    async (kind) => {
      vi.mocked(copyToClipboard).mockResolvedValue(true);
      const sub = `Traceback\n${"details ".repeat(100)}\nlast line`;
      useToastStore.getState().push({ msg: "任务失败", sub, kind });
      const call = vi.mocked(toast[kind]).mock.calls[0];
      const options = call[1]!;
      expect(options.description).toBe(sub);
      const preventDefault = vi.fn();
      const action = options.action as { onClick: (event: unknown) => void };
      await action.onClick({ preventDefault });
      expect(preventDefault).toHaveBeenCalled();
      expect(copyToClipboard).toHaveBeenCalledWith(`任务失败\n${sub}`);
      expect(toast.success).toHaveBeenCalledWith("已复制完整错误信息");
    },
  );

  it("copies error titles without details and reports clipboard failure", async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(false);
    useToastStore.getState().push({ msg: "网络失败", kind: "error" });
    const action = vi.mocked(toast.error).mock.calls[0][1]!.action as {
      onClick: (event: unknown) => void;
    };
    await action.onClick({ preventDefault: vi.fn() });
    expect(copyToClipboard).toHaveBeenCalledWith("网络失败");
    expect(toast.error).toHaveBeenLastCalledWith("复制失败，请检查浏览器剪贴板权限");
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("keeps ordinary success notifications free of error actions", () => {
    useToastStore.getState().push({ msg: "已保存", sub: "配置生效", kind: "success" });
    expect(vi.mocked(toast.success).mock.calls[0][1]).not.toHaveProperty("action");
  });

  it("copies diagnostic details from legacy notifications without an explicit kind", async () => {
    vi.mocked(copyToClipboard).mockResolvedValue(true);
    useToastStore.getState().push({ msg: "操作失败", sub: "完整服务错误" });
    const action = vi.mocked(toast).mock.calls[0][1]!.action as {
      onClick: (event: unknown) => void;
    };
    await action.onClick({ preventDefault: vi.fn() });
    expect(copyToClipboard).toHaveBeenCalledWith("操作失败\n完整服务错误");
  });
});
