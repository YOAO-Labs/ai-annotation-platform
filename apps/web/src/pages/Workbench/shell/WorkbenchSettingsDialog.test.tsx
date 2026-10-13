import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_WORKBENCH_PREFERENCES } from "@/api/auth";
import { POINT_CLOUD_WEBGPU_STORAGE_KEY } from "../state/workbenchSettingsFields";
import { WorkbenchSettingsDialog } from "./WorkbenchSettingsDialog";

const mocks = vi.hoisted(() => ({
  setFields: vi.fn(),
  retryLoad: vi.fn(),
  loaded: true,
  loadError: null as Error | null,
  lockedFields: [] as string[],
}));
const preferencesMock = vi.hoisted(() => ({
  data: {
    items: [
      { type: "task.rejected", in_app: true, email: false, toast: true },
      { type: "task.approved", in_app: true, email: false, toast: false },
    ],
  },
  isPending: false,
  isError: false,
  isSuccess: true,
  refetch: vi.fn(),
}));
vi.mock("@/hooks/useMediaQuery", () => ({ useMediaQuery: () => true }));
vi.mock("../state/useWorkbenchConfig", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useWorkbenchConfig: () => ({ config: DEFAULT_WORKBENCH_PREFERENCES, ...mocks }),
}));
vi.mock("@/hooks/useNotificationPreferences", () => ({
  useNotificationPreferences: () => preferencesMock,
  useUpdateNotificationPreference: () => ({ mutateAsync: vi.fn().mockResolvedValue({ ok: true }) }),
}));

function withProviders(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{ui}</QueryClientProvider>;
}

type Props = Partial<Parameters<typeof WorkbenchSettingsDialog>[0]>;
function mount(props: Props = {}) {
  return render(
    withProviders(
      <MemoryRouter>
        <WorkbenchSettingsDialog open onClose={vi.fn()} {...props} />
      </MemoryRouter>,
    ),
  );
}
const category = (name: string) => screen.getByRole("tab", { name });

describe("WorkbenchSettingsDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.loaded = true;
    mocks.loadError = null;
    mocks.lockedFields = [];
    preferencesMock.isPending = false;
    preferencesMock.isError = false;
    preferencesMock.isSuccess = true;
    localStorage.clear();
  });

  it("previews slider changes immediately and restores the dialog on capture loss", () => {
    mount();
    const slider = screen.getByRole("slider", { name: "左栏宽度" });
    fireEvent.pointerDown(slider, { pointerId: 1 });
    expect(screen.getByTestId("workbench-settings-dialog")).toHaveAttribute(
      "data-previewing",
      "true",
    );
    fireEvent.change(slider, { target: { value: "22" } });
    expect(mocks.setFields).toHaveBeenCalledWith({ common: { leftWidthPct: 22 } });
    fireEvent.lostPointerCapture(slider);
    expect(screen.getByTestId("workbench-settings-dialog")).not.toHaveAttribute("data-previewing");
    expect(mocks.setFields).toHaveBeenCalledTimes(1);
  });

  it("shows all seven purpose categories and leaves hidden fields unavailable", () => {
    mount();
    expect(
      within(screen.getByRole("tablist", { name: "设置分类" }))
        .getAllByRole("tab")
        .map((tab) => tab.textContent),
    ).toEqual([
      "界面布局",
      "标注显示",
      "编辑与辅助",
      "画布与视角",
      "播放与轨迹",
      "性能与实验",
      "通知",
    ]);
    expect(screen.queryByText("网格吸附")).not.toBeInTheDocument();
  });

  it("edits video and pointcloud preferences from the same dialog", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(category("播放与轨迹"));
    await user.selectOptions(screen.getByRole("combobox", { name: "默认播放速率" }), "2");
    await user.click(category("画布与视角"));
    await user.click(screen.getByRole("switch", { name: "显示地面网格" }));
    expect(mocks.setFields.mock.calls).toEqual([
      [{ video: { defaultPlaybackRate: 2 } }],
      [{ pointcloud: { showGrid: false } }],
    ]);
  });

  it("closed dialog renders nothing", () => {
    mount({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("switches categories, preserves project locks and writes unlocked fields", async () => {
    mocks.lockedFields = ["smoothImage"];
    mount();
    const user = userEvent.setup();
    await user.click(category("画布与视角"));
    const smooth = screen.getByTestId("setting-field-image.smoothImage");
    expect(within(smooth).getByRole("switch")).toBeDisabled();
    expect(within(smooth).getByText("项目锁定")).toBeVisible();
    await user.click(category("编辑与辅助"));
    await user.selectOptions(screen.getByRole("combobox", { name: "画框后行为" }), "reuse_active");
    expect(mocks.setFields).toHaveBeenCalledWith({ image: { afterBoxCreate: "reuse_active" } });
  });

  it("search spans categories, keeps the disabled child's parent, and clears on category click", async () => {
    mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "搜索设置" }), "上色 Gamma");
    expect(screen.getByTestId("setting-field-pointcloud.colorizeWithCamera")).toBeVisible();
    expect(
      within(screen.getByTestId("setting-field-pointcloud.colorizeGamma")).getByRole("slider"),
    ).toBeDisabled();
    expect(screen.queryByTestId("setting-field-pointcloud.colorizeBrightness")).toBeNull();
    await user.click(category("界面布局"));
    expect(screen.getByRole("textbox", { name: "搜索设置" })).toHaveValue("");
    await user.type(screen.getByRole("textbox", { name: "搜索设置" }), "不存在的设置");
    expect(screen.getByText("没有找到相关设置")).toBeVisible();
  });

  it("keeps the overlay blurred by default and unblurred on a live 3D canvas", () => {
    const view = mount();
    expect(screen.getByTestId("workbench-settings-overlay")).toHaveClass("backdrop-blur-overlay");
    view.rerender(
      withProviders(
        <MemoryRouter>
          <WorkbenchSettingsDialog open onClose={vi.fn()} backdropBlur={false} />
        </MemoryRouter>,
      ),
    );
    const overlay = screen.getByTestId("workbench-settings-overlay");
    expect(overlay).not.toHaveClass("backdrop-blur-overlay");
    expect(overlay).toHaveClass("bg-black/25");
  });

  it("highlights matched terms and keeps section legends above field labels", async () => {
    mount();
    const user = userEvent.setup();
    const legend = document.querySelector('[data-slot="field-legend"]');
    expect(legend).toHaveAttribute("data-variant", "legend");
    expect(legend?.className).toContain("data-[variant=legend]:text-md");
    await user.type(screen.getByRole("textbox", { name: "搜索设置" }), "Gamma");
    expect(screen.getAllByText("Gamma", { selector: "mark" }).length).toBeGreaterThan(0);
  });

  it("writes every local experiment and both special settings without a modality filter", async () => {
    const onToggleHideOrphans = vi.fn();
    const onToggleSecondaryBar = vi.fn();
    mount({ onToggleHideOrphans, onToggleSecondaryBar });
    const user = userEvent.setup();
    await user.click(category("编辑与辅助"));
    await user.click(screen.getByRole("switch", { name: "隐藏孤儿标注" }));
    await user.click(screen.getByRole("switch", { name: "二次推理面板" }));
    expect(onToggleSecondaryBar).toHaveBeenCalledOnce();
    expect(onToggleHideOrphans).toHaveBeenCalledOnce();
    await user.click(category("性能与实验"));
    await user.click(screen.getByRole("switch", { name: "3D WebGPU 渲染器" }));
    expect(localStorage.getItem(POINT_CLOUD_WEBGPU_STORAGE_KEY)).toBe("1");
    expect(mocks.setFields).not.toHaveBeenCalled();
    await user.click(screen.getByRole("switch", { name: "WebCodecs 精确解码" }));
    expect(localStorage.getItem("video.experimental.webcodecs")).toBe("0");
    expect(screen.getByRole("combobox", { name: "参考框运动预测" })).toBeVisible();
    expect(mocks.setFields).not.toHaveBeenCalled();
  });

  it("secondary toggle keeps its independent callback", async () => {
    const onToggleSecondaryBar = vi.fn();
    mount({ onToggleSecondaryBar });
    const user = userEvent.setup();
    await user.click(category("编辑与辅助"));
    await user.click(screen.getByRole("switch", { name: "二次推理面板" }));
    expect(onToggleSecondaryBar).toHaveBeenCalledOnce();
    expect(mocks.setFields).not.toHaveBeenCalled();
  });

  it("an outside click commits the text, closes and restores focus without reaching the background", async () => {
    const background = vi.fn();
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)}>打开设置</button>
          <button onClick={background}>画布</button>
          <WorkbenchSettingsDialog open={open} onClose={() => setOpen(false)} />
        </>
      );
    }
    render(
      withProviders(
        <MemoryRouter>
          <Harness />
        </MemoryRouter>,
      ),
    );
    const user = userEvent.setup();
    const trigger = screen.getByRole("button", { name: "打开设置" });
    await user.click(trigger);
    await user.click(category("画布与视角"));
    await user.type(screen.getByRole("textbox", { name: "CSS 图像滤镜" }), " invert(1) ");
    await user.click(screen.getByTestId("workbench-settings-overlay"));
    expect(mocks.setFields).toHaveBeenCalledTimes(1);
    expect(mocks.setFields).toHaveBeenCalledWith({ image: { cssImageFilter: "invert(1)" } });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(background).not.toHaveBeenCalled();
    expect(trigger).toHaveFocus();
  });

  it("Esc commits focused settings but composition and inside-to-outside drag do not close", async () => {
    const onClose = vi.fn();
    mount({ onClose });
    const user = userEvent.setup();
    await user.click(category("画布与视角"));
    const input = screen.getByRole("textbox", { name: "CSS 图像滤镜" });
    await user.type(input, "contrast(1.2)");
    fireEvent.compositionStart(input);
    fireEvent.keyDown(input, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.compositionEnd(input);
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledOnce();
    expect(mocks.setFields).toHaveBeenCalledWith({ image: { cssImageFilter: "contrast(1.2)" } });
    onClose.mockClear();
    await user.click(category("编辑与辅助"));
    fireEvent.pointerDown(screen.getByRole("slider", { name: "控制点大小" }));
    fireEvent.pointerUp(screen.getByTestId("workbench-settings-overlay"));
    fireEvent.click(screen.getByTestId("workbench-settings-overlay"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("keeps focus in the dialog and commits on category change", async () => {
    mount();
    const user = userEvent.setup();
    await user.click(category("画布与视角"));
    await user.type(screen.getByRole("textbox", { name: "CSS 图像滤镜" }), "brightness(1.1)");
    await user.click(category("界面布局"));
    expect(mocks.setFields).toHaveBeenCalledTimes(1);
    expect(mocks.setFields).toHaveBeenCalledWith({ image: { cssImageFilter: "brightness(1.1)" } });
    screen.getByRole("button", { name: "关闭设置" }).focus();
    await user.tab();
    expect(screen.getByRole("dialog")).toContainElement(document.activeElement as HTMLElement);
  });

  it("failed loads show retry and no writable defaults", async () => {
    mocks.loadError = new Error("offline");
    mount();
    expect(screen.queryByRole("slider")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(mocks.retryLoad).toHaveBeenCalledOnce();
  });
});

describe("WorkbenchSettingsDialog · 通知分类", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.loaded = true;
    mocks.loadError = null;
    preferencesMock.isPending = false;
    preferencesMock.isError = false;
    preferencesMock.isSuccess = true;
  });

  it("通知分类渲染共享偏好面板，不受渲染配置加载失败影响", async () => {
    mocks.loadError = new Error("render config down");
    mount();
    const user = userEvent.setup();
    await user.click(category("通知"));
    expect(await screen.findByTestId("notification-preferences-panel")).toBeVisible();
    expect(screen.getByText("任务被退回")).toBeVisible();
    // 渲染配置的重试按钮不吞掉通知面板
    expect(screen.getByTestId("notification-preference-task.rejected")).toBeVisible();
  });

  it("偏好加载失败时通知分类显示自己的错误与重试", async () => {
    preferencesMock.isPending = false;
    preferencesMock.isError = true;
    preferencesMock.isSuccess = false;
    mount();
    const user = userEvent.setup();
    await user.click(category("通知"));
    expect(await screen.findByRole("alert")).toHaveTextContent("无法加载通知偏好");
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(preferencesMock.refetch).toHaveBeenCalled();
  });

  it("全局搜索命中通知标签时展示通知偏好结果", async () => {
    mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "搜索设置" }), "任务被退回");
    expect(await screen.findByTestId("notification-preferences-panel")).toBeVisible();
    expect(screen.getAllByText("通知偏好").length).toBeGreaterThan(0);
  });

  it.each(["通知", "通知偏好"])("全局搜索分类「%s」时展示分类内的设置项", async (query) => {
    mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "搜索设置" }), query);
    expect(screen.getByTestId("notification-preference-task.rejected")).toBeVisible();
    expect(screen.getByTestId("notification-preference-task.approved")).toBeVisible();
    expect(screen.queryByText("没有匹配的通知类型。")).toBeNull();
  });

  it("搜索无命中且通知也不匹配时仍显示空态", async () => {
    mount();
    const user = userEvent.setup();
    await user.type(screen.getByRole("textbox", { name: "搜索设置" }), "不存在的设置");
    expect(await screen.findByText("没有找到相关设置")).toBeVisible();
  });
});
