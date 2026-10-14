/**
 * decisionDialog 服务 + <DecisionDialogHost /> 单测(docs/plans/archive/1789527942 Phase 0 验收):
 * Promise 结算(确认/取消/Esc/点遮罩)、危险态默认聚焦取消 vs 普通态聚焦确认、
 * 危险态语义 token、input 必填/maxLength/validate/initialValue 行为、choice 取消为 null、
 * 队列一次只显示一条、data-modal 模态标记、cancelAll 认证归属变更兜底。
 */
import { afterEach, describe, expect, it } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { DecisionDialogHost } from "./DecisionDialogHost";
import {
  alertDialog,
  choiceDialog,
  confirmDialog,
  inputDialog,
  useDecisionDialogStore,
} from "./decisionDialog";

function renderHost() {
  return render(<DecisionDialogHost />);
}

function getOverlay() {
  const overlay = document.querySelector('[data-slot="alert-dialog-overlay"]');
  expect(overlay).not.toBeNull();
  return overlay as Element;
}

/** 服务入队即 store 更新,包一层 act 避免测试告警;同步冲出首帧 DOM。 */
function openDialog<T>(open: () => T): T {
  let result!: T;
  act(() => {
    result = open();
  });
  return result;
}

/**
 * 触发关闭后,把 Radix Presence 的 rAF 卸载帧(jsdom 约 16ms)放进 act 里冲掉,
 * 让「退场完毕出队」的 store 更新落在 act 内,避免测试告警。
 */
async function flushTeardown() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 50));
  });
}

// vitest 的 afterEach 按 LIFO 在 RTL cleanup 前跑,此刻 Host 仍挂载,清空队列需包 act。
afterEach(() => {
  act(() => {
    useDecisionDialogStore.setState({ queue: [] });
  });
});

describe("decisionDialog 服务", () => {
  it("无待决请求时 Host 不渲染任何对话框", () => {
    renderHost();
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });

  it("confirmDialog:确认 → true", async () => {
    renderHost();
    const promise = openDialog(() => confirmDialog({ title: "删除数据集", confirmLabel: "删除" }));
    const dialog = await screen.findByRole("alertdialog", { name: "删除数据集" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    await flushTeardown();
    await expect(promise).resolves.toBe(true);
  });

  it("confirmDialog:点击取消 → false", async () => {
    renderHost();
    const promise = openDialog(() => confirmDialog({ title: "删除数据集", confirmLabel: "删除" }));
    const dialog = await screen.findByRole("alertdialog", { name: "删除数据集" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await flushTeardown();
    await expect(promise).resolves.toBe(false);
  });

  it("confirmDialog:Esc → false", async () => {
    renderHost();
    const promise = openDialog(() => confirmDialog({ title: "删除数据集", confirmLabel: "删除" }));
    await screen.findByRole("alertdialog", { name: "删除数据集" });
    fireEvent.keyDown(document.body, { key: "Escape" });
    await flushTeardown();
    await expect(promise).resolves.toBe(false);
  });

  it("confirmDialog:点遮罩 → false", async () => {
    renderHost();
    const promise = openDialog(() => confirmDialog({ title: "删除数据集", confirmLabel: "删除" }));
    await screen.findByRole("alertdialog", { name: "删除数据集" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    fireEvent.pointerDown(getOverlay());
    await flushTeardown();
    await expect(promise).resolves.toBe(false);
  });

  it("confirmDialog:description/details 正常渲染", async () => {
    renderHost();
    openDialog(() =>
      confirmDialog({
        title: "删除数据集",
        description: "删除后无法恢复",
        details: "将删除 3 个数据集 · 1,204 张图片",
        confirmLabel: "删除",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "删除数据集" });
    expect(within(dialog).getByText("删除后无法恢复")).toBeInTheDocument();
    expect(within(dialog).getByText("将删除 3 个数据集 · 1,204 张图片")).toBeInTheDocument();
  });

  it("危险 confirm 默认聚焦取消,普通 confirm 默认聚焦确认", async () => {
    renderHost();
    openDialog(() =>
      confirmDialog({ tone: "danger", title: "撤销访问密钥", confirmLabel: "撤销" }),
    );
    await screen.findByRole("alertdialog", { name: "撤销访问密钥" });
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();

    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    await flushTeardown();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());

    openDialog(() => confirmDialog({ title: "提交批次", confirmLabel: "提交" }));
    await screen.findByRole("alertdialog", { name: "提交批次" });
    expect(screen.getByRole("button", { name: "提交" })).toHaveFocus();
  });

  it("危险态走语义 status token:图标容器柔和底色、确认按钮描边文字色,无整块红", async () => {
    renderHost();
    openDialog(() =>
      confirmDialog({ tone: "danger", title: "撤销访问密钥", confirmLabel: "撤销" }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "撤销访问密钥" });
    const media = document.querySelector('[data-slot="alert-dialog-media"]');
    expect(media).toHaveClass("bg-status-danger-soft", "text-status-danger");
    const action = within(dialog).getByRole("button", { name: "撤销" });
    expect(action).toHaveClass("text-status-danger", "border-status-danger/30");
    expect(action).not.toHaveClass("bg-destructive");
  });

  it("inputDialog:必填为空提交 → 行内报错不关窗,补填后提交 → 返回去空白值", async () => {
    renderHost();
    const promise = openDialog(() =>
      inputDialog({
        title: "整批退回",
        label: "退回原因",
        required: true,
        maxLength: 500,
        confirmLabel: "确认驳回",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "整批退回" });
    const textarea = within(dialog).getByLabelText(/退回原因/);
    expect(textarea).toHaveAttribute("maxlength", "500");

    fireEvent.click(within(dialog).getByRole("button", { name: "确认驳回" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(/请填写/);
    expect(screen.getByRole("alertdialog", { name: "整批退回" })).toBeInTheDocument();

    fireEvent.change(textarea, { target: { value: "  图片质量差  " } });
    expect(within(dialog).queryByRole("alert")).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: "确认驳回" }));
    await flushTeardown();
    await expect(promise).resolves.toBe("图片质量差");
  });

  it("inputDialog:validate 返回错误文案时就地报错,通过后返回填写值", async () => {
    renderHost();
    const promise = openDialog(() =>
      inputDialog({
        title: "重命名标签",
        label: "新名称",
        confirmLabel: "重命名",
        validate: (value) => (value.length < 2 ? "至少 2 个字符" : null),
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "重命名标签" });
    const textarea = within(dialog).getByLabelText(/新名称/);
    fireEvent.change(textarea, { target: { value: "x" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重命名" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("至少 2 个字符");
    expect(dialog).toHaveAttribute("data-state", "open");

    fireEvent.change(textarea, { target: { value: "车辆" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "重命名" }));
    await flushTeardown();
    await expect(promise).resolves.toBe("车辆");
  });

  it("inputDialog:取消/Esc → null", async () => {
    renderHost();
    const cancelled = openDialog(() =>
      inputDialog({ title: "整批退回", label: "退回原因", confirmLabel: "确认驳回" }),
    );
    let dialog = await screen.findByRole("alertdialog", { name: "整批退回" });
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await flushTeardown();
    await expect(cancelled).resolves.toBeNull();

    const escaped = openDialog(() =>
      inputDialog({ title: "整批退回 2", label: "退回原因", confirmLabel: "确认驳回" }),
    );
    dialog = await screen.findByRole("alertdialog", { name: "整批退回 2" });
    fireEvent.keyDown(document.body, { key: "Escape" });
    await flushTeardown();
    await expect(escaped).resolves.toBeNull();
  });

  it("choiceDialog:点击选项 → 返回 key;Esc → null;危险选项走语义 token", async () => {
    renderHost();
    const promise = openDialog(() =>
      choiceDialog({
        title: "离开 Mask 编辑",
        description: "当前对象有未保存的修改",
        options: [
          { key: "save", label: "保存并离开" },
          { key: "discard", label: "丢弃修改", tone: "danger" },
          { key: "continue", label: "继续编辑" },
        ],
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "离开 Mask 编辑" });
    expect(within(dialog).getByText("当前对象有未保存的修改")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "丢弃修改" })).toHaveClass(
      "text-status-danger",
      "border-status-danger/30",
    );
    fireEvent.click(within(dialog).getByRole("button", { name: "丢弃修改" }));
    await flushTeardown();
    await expect(promise).resolves.toBe("discard");

    const escaped = openDialog(() =>
      choiceDialog({
        title: "清空栅格",
        options: [
          { key: "apply", label: "确认清空" },
          { key: "back", label: "返回预览" },
        ],
      }),
    );
    await screen.findByRole("alertdialog", { name: "清空栅格" });
    fireEvent.keyDown(document.body, { key: "Escape" });
    await flushTeardown();
    await expect(escaped).resolves.toBeNull();
  });

  it("alertDialog:渲染标题/描述,确认 → void", async () => {
    renderHost();
    const promise = openDialog(() =>
      alertDialog({
        title: "文件类型不支持",
        description: "请选择 .zip 文件",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "文件类型不支持" });
    expect(within(dialog).getByText("请选择 .zip 文件")).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "确定" }));
    await flushTeardown();
    await expect(promise).resolves.toBeUndefined();
  });

  it("队列一次只渲染一条,前一条结算并退场后下一条才接管", async () => {
    renderHost();
    const first = openDialog(() => confirmDialog({ title: "第一个决策", confirmLabel: "继续" }));
    const second = openDialog(() => confirmDialog({ title: "第二个决策", confirmLabel: "继续" }));

    await screen.findByRole("alertdialog", { name: "第一个决策" });
    expect(screen.getAllByRole("alertdialog")).toHaveLength(1);
    expect(screen.queryByRole("alertdialog", { name: "第二个决策" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "继续" }));
    await flushTeardown();
    await expect(first).resolves.toBe(true);

    const secondDialog = await screen.findByRole("alertdialog", { name: "第二个决策" });
    expect(screen.getAllByRole("alertdialog")).toHaveLength(1);
    fireEvent.click(within(secondDialog).getByRole("button", { name: "继续" }));
    await flushTeardown();
    await expect(second).resolves.toBe(true);
  });

  it("对话框内容带 data-modal 标记,供 isWorkbenchInteractionBlocked 识别", async () => {
    renderHost();
    openDialog(() => confirmDialog({ title: "删除数据集", confirmLabel: "删除" }));
    const dialog = await screen.findByRole("alertdialog", { name: "删除数据集" });
    // Workbench 的窗口级快捷键守卫靠 [data-modal] 拦截,缺标记时 A/R 键会穿透对话框。
    expect(dialog).toHaveAttribute("data-modal", "");
  });

  it("inputDialog:initialValue 预填可改写,提交返回改写值", async () => {
    renderHost();
    const promise = openDialog(() =>
      inputDialog({
        title: "补充说明",
        label: "补充说明（可选）",
        initialValue: "标注员跳过：目标不在画面内",
        confirmLabel: "确认退回",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "补充说明" });
    const textarea = within(dialog).getByLabelText(/补充说明/);
    expect(textarea).toHaveValue("标注员跳过：目标不在画面内");

    fireEvent.change(textarea, { target: { value: "标注员跳过：目标不可见" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "确认退回" }));
    await flushTeardown();
    await expect(promise).resolves.toBe("标注员跳过：目标不可见");
  });

  it("inputDialog:presets 快捷标签气泡点击可快速追加到输入框", async () => {
    renderHost();
    const promise = openDialog(() =>
      inputDialog({
        title: "补充说明",
        label: "补充说明（可选）",
        presets: ["边缘漏标", "重叠遮挡未拆分"],
        confirmLabel: "确认退回",
      }),
    );
    const dialog = await screen.findByRole("alertdialog", { name: "补充说明" });
    const textarea = within(dialog).getByLabelText(/补充说明/);
    expect(textarea).toHaveValue("");

    const preset1 = within(dialog).getByRole("button", { name: "+ 边缘漏标" });
    const preset2 = within(dialog).getByRole("button", { name: "+ 重叠遮挡未拆分" });
    fireEvent.click(preset1);
    expect(textarea).toHaveValue("边缘漏标");

    fireEvent.click(preset2);
    expect(textarea).toHaveValue("边缘漏标；重叠遮挡未拆分");

    // 重复点击同一标签不重复添加
    fireEvent.click(preset1);
    expect(textarea).toHaveValue("边缘漏标；重叠遮挡未拆分");

    fireEvent.click(within(dialog).getByRole("button", { name: "确认退回" }));
    await flushTeardown();
    await expect(promise).resolves.toBe("边缘漏标；重叠遮挡未拆分");
  });

  it("cancelAll:按各 kind 的取消值结算整条队列并清空(认证归属变更兜底)", async () => {
    renderHost();
    const confirmed = openDialog(() =>
      confirmDialog({ title: "删除数据集", confirmLabel: "删除" }),
    );
    const commented = openDialog(() =>
      inputDialog({ title: "补充说明", label: "补充说明", confirmLabel: "提交" }),
    );
    await screen.findByRole("alertdialog", { name: "删除数据集" });

    act(() => {
      useDecisionDialogStore.getState().cancelAll();
    });
    await expect(confirmed).resolves.toBe(false);
    await expect(commented).resolves.toBeNull();
    await waitFor(() => expect(screen.queryByRole("alertdialog")).toBeNull());
    expect(useDecisionDialogStore.getState().queue).toHaveLength(0);
  });
});
