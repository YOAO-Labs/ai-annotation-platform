import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import { server } from "@/mocks/server";

import { AvatarPickerDialog } from "./AvatarPickerDialog";

const MANIFEST = {
  style: "dicebear/pixel-art",
  creator: "DiceBear",
  license: "CC0 1.0",
  licenseUrl: "https://creativecommons.org/publicdomain/zero/1.0/",
  sourceUrl: "https://www.dicebear.com/styles/pixel-art/",
  items: [
    { id: "pixel-01", label: "像素头像 01" },
    { id: "pixel-02", label: "像素头像 02" },
    { id: "pixel-03", label: "像素头像 03" },
  ],
};

function renderDialog(props: Partial<React.ComponentProps<typeof AvatarPickerDialog>> = {}) {
  const onSelect = vi.fn();
  const onClear = vi.fn();
  const onOpenChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AvatarPickerDialog
        open
        onOpenChange={onOpenChange}
        currentRef="preset:pixel-02"
        onSelect={onSelect}
        onClear={onClear}
        {...props}
      />
    </QueryClientProvider>,
  );
  return { onSelect, onClear, onOpenChange };
}

describe("AvatarPickerDialog", () => {
  it("列出内置头像并在选中项上标记 pressed", async () => {
    server.use(http.get("*/avatars/pixel/manifest.json", () => HttpResponse.json(MANIFEST)));
    renderDialog();

    const option = await screen.findByRole("button", { name: "像素头像 01" });
    expect(option).toBeInTheDocument();
    expect(option.querySelector("img")).toHaveAttribute("src", "/avatars/pixel/pixel-01.svg");
    expect(screen.getByRole("button", { name: "像素头像 02" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(option).toHaveAttribute("aria-pressed", "false");
  });

  it("点击某个头像后回传 preset 引用", async () => {
    server.use(http.get("*/avatars/pixel/manifest.json", () => HttpResponse.json(MANIFEST)));
    const { onSelect } = renderDialog();

    (await screen.findByRole("button", { name: "像素头像 03" })).click();
    expect(onSelect).toHaveBeenCalledWith("preset:pixel-03");
  });

  it("提交中禁用重复点击", async () => {
    server.use(http.get("*/avatars/pixel/manifest.json", () => HttpResponse.json(MANIFEST)));
    const { onSelect } = renderDialog({ pending: true });

    const option = await screen.findByRole("button", { name: "像素头像 01" });
    expect(option).toBeDisabled();
    option.click();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("当前无头像时禁用「恢复默认」", async () => {
    server.use(http.get("*/avatars/pixel/manifest.json", () => HttpResponse.json(MANIFEST)));
    const { onClear } = renderDialog({ currentRef: null });

    const reset = await screen.findByRole("button", { name: "恢复默认" });
    expect(reset).toBeDisabled();
    reset.click();
    expect(onClear).not.toHaveBeenCalled();
  });

  it("目录加载失败时提示且不抛出", async () => {
    server.use(
      http.get("*/avatars/pixel/manifest.json", () => new HttpResponse(null, { status: 500 })),
    );
    renderDialog();

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("头像目录加载失败"));
  });
});
