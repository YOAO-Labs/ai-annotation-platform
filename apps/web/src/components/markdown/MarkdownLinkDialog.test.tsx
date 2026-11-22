import {
  defaultSvgIcons,
  editorRootElementRef$,
  iconComponentFor$,
  linkDialogState$,
  readOnly$,
  Realm,
  RealmContext,
  translation$,
  updateLink$,
} from "@mdxeditor/editor";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { MarkdownLinkDialog, MarkdownLinkSuggestions } from "./MarkdownLinkDialog";
import { translateMarkdownEditor } from "./markdownEditorLabels";

function fixture(label: string, suggestions: string[], readOnly = false) {
  const realm = new Realm();
  const submit = vi.fn();
  realm.sub(updateLink$, submit);
  realm.pub(translation$, translateMarkdownEditor);
  realm.pub(iconComponentFor$, (name) => defaultSvgIcons[name]);
  realm.pub(readOnly$, readOnly);
  const open = () => {
    realm.pub(linkDialogState$, {
      type: "edit",
      initialUrl: "",
      url: "",
      title: "",
      text: "",
      withAnchorText: true,
      linkNodeKey: "",
      rectangle: { top: 10, left: 10, width: 0, height: 20 },
    });
  };
  return {
    submit,
    open,
    element: (
      <section aria-label={label}>
        <div ref={(element) => realm.pub(editorRootElementRef$, { current: element })} />
        <RealmContext.Provider value={realm}>
          <MarkdownLinkSuggestions.Provider value={suggestions}>
            <MarkdownLinkDialog />
          </MarkdownLinkSuggestions.Provider>
        </RealmContext.Provider>
      </section>
    ),
  };
}

function scope(name: string) {
  return within(screen.getByRole("region", { name }));
}

describe("Markdown link dialog", () => {
  it("names the plain textbox, tabs through the form, and submits typed values", async () => {
    const user = userEvent.setup();
    const editor = fixture("编辑器", []);
    render(editor.element);
    act(editor.open);
    const dialog = scope("编辑器");
    const address = await dialog.findByRole("textbox", { name: "地址" });
    await waitFor(() => expect(address).toHaveFocus());
    await user.type(address, "https://example.com/custom");
    await user.tab();
    const text = dialog.getByRole("textbox", { name: "文本" });
    expect(text).toHaveFocus();
    await user.type(text, "自定义链接");
    await user.tab();
    const title = dialog.getByRole("textbox", { name: "链接标题" });
    expect(title).toHaveFocus();
    await user.type(title, "说明");
    await user.tab();
    expect(dialog.getByRole("button", { name: "保存链接" })).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(editor.submit).toHaveBeenCalledTimes(1);
    expect(editor.submit).toHaveBeenCalledWith({
      url: "https://example.com/custom",
      text: "自定义链接",
      title: "说明",
    });
  });

  it("isolates two dialogs and selects a scoped suggestion without submitting until the next Enter", async () => {
    const user = userEvent.setup();
    const first = fixture("第一编辑器", ["https://first.example/one"]);
    const second = fixture("第二编辑器", [
      "https://second.example/one",
      "https://second.example/two",
    ]);
    render(
      <>
        {first.element}
        {second.element}
      </>,
    );
    act(first.open);
    const firstDialog = scope("第一编辑器");
    const firstAddress = await firstDialog.findByRole("combobox", { name: "地址" });
    await waitFor(() => expect(firstAddress).toHaveFocus());
    act(second.open);
    const secondDialog = scope("第二编辑器");
    const secondAddress = await secondDialog.findByRole("combobox", { name: "地址" });
    await waitFor(() => expect(secondAddress).toHaveFocus());
    expect(firstAddress.id).not.toBe(secondAddress.id);
    expect(firstAddress).toHaveValue("");
    await user.type(secondAddress, "SECOND");
    expect(secondDialog.getByRole("listbox", { name: "地址" })).toBeVisible();
    expect(secondDialog.getAllByRole("option")).toHaveLength(2);
    await user.keyboard("{ArrowDown}{ArrowDown}");
    const option = secondDialog.getByRole("option", {
      name: "https://second.example/two",
      selected: true,
    });
    expect(secondAddress).toHaveAttribute("aria-activedescendant", option.id);
    await user.keyboard("{Enter}");
    expect(secondAddress).toHaveValue("https://second.example/two");
    expect(secondAddress).toHaveAttribute("aria-expanded", "false");
    expect(second.submit).not.toHaveBeenCalled();
    await user.keyboard("{Enter}");
    expect(second.submit).toHaveBeenCalledTimes(1);
    expect(second.submit).toHaveBeenCalledWith({
      url: "https://second.example/two",
      text: "",
      title: "",
    });
    expect(first.submit).not.toHaveBeenCalled();
    expect(firstAddress).toHaveValue("");
  });

  it("does not render an edit form even if a read-only realm receives an edit state", () => {
    const editor = fixture("只读编辑器", ["https://example.com"], true);
    render(editor.element);
    act(editor.open);
    expect(scope("只读编辑器").queryByRole("dialog")).not.toBeInTheDocument();
  });
});
