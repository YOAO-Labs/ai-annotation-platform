import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { MarkdownView } from "./MarkdownView";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("MarkdownView", () => {
  it("explains that an unverified legacy image needs to be uploaded again", async () => {
    const resolveImage = vi.fn().mockRejectedValue(
      Object.assign(new Error("unknown"), {
        status: 409,
        detailRaw: { code: "guide_asset_storage_unpinned" },
      }),
    );
    render(<MarkdownView content="![旧图](guide-asset:legacy)" resolveImage={resolveImage} />);
    expect(await screen.findByText("图片无法核验，请重新上传")).toBeVisible();
    expect(screen.getByTestId("markdown-image-retry")).toBeVisible();
  });

  it("keeps a busy storage conflict retryable without asking for a replacement upload", async () => {
    const resolveImage = vi.fn().mockRejectedValue(
      Object.assign(new Error("busy"), {
        status: 409,
        detailRaw: { code: "guide_asset_busy" },
      }),
    );
    render(<MarkdownView content="![图片](guide-asset:key)" resolveImage={resolveImage} />);
    expect(await screen.findByText("图片暂时不可用")).toBeVisible();
    expect(screen.queryByText(/重新上传/)).not.toBeInTheDocument();
  });
  it("renders GFM tables, task lists, ordered-list starts, and safe links", () => {
    render(
      <MarkdownView
        content={
          "# 标题\n\n| 左 | 右 |\n| :- | -: |\n| a | b |\n\n2. 第二项\n3. 第三项\n\n- [x] 已完成\n\n[危险](javascript:alert(1))"
        }
      />,
    );

    expect(screen.getByRole("heading", { level: 1, name: "标题" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "左" })).toHaveStyle({ textAlign: "left" });
    expect(screen.getByRole("columnheader", { name: "右" })).toHaveStyle({ textAlign: "right" });
    expect(document.querySelector("ol")).toHaveAttribute("start", "2");
    expect(screen.getByRole("checkbox")).toBeChecked();
    expect(screen.getByText("危险").closest("a")).toHaveAttribute("href", "");
  });

  it("does not render raw HTML and copies fenced code", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <MarkdownView
        content={'<script>alert("xss")</script>\n\n```typescript\nconst value = "<safe>";\n```'}
      />,
    );

    expect(screen.queryByText(/alert/)).not.toBeInTheDocument();
    expect(screen.getByText("typescript")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制代码" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('const value = "<safe>";'));
  });

  it("supports reference guide images and enlarges them without nesting interactions", async () => {
    const gate = deferred<{ url: string; expiresAt: number }>();
    const resolveImage = vi.fn().mockReturnValue(gate.promise);
    render(
      <MarkdownView
        content={"![参考图][diagram]\n\n[diagram]: guide-asset:project/diagram.png"}
        resolveImage={resolveImage}
        imageScope="project-1"
      />,
    );

    expect(screen.getByRole("status", { name: "加载图片中" })).toBeInTheDocument();
    expect(resolveImage).toHaveBeenCalledWith("guide-asset:project/diagram.png");

    gate.resolve({ url: "https://cdn.example/diagram.png", expiresAt: Date.now() + 60_000 });
    const image = await screen.findByAltText("参考图");
    fireEvent.load(image);
    fireEvent.click(screen.getByRole("button", { name: "放大图片：参考图" }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("参考图");
  });

  it("preserves Unicode, spaces, and literal percent signs in direct and reference guide keys", async () => {
    const directSource = "guide-asset:projects/p/guide/asset-中文 文件%20%.png";
    const referenceSource = "guide-asset:projects/p/guide/ref-中文 文件%20%.png";
    const resolveImage = vi.fn().mockResolvedValue({
      url: "https://cdn.example/guide.png",
      expiresAt: Date.now() + 60_000,
    });
    render(
      <MarkdownView
        content={`![直接图](<${directSource}>)\n\n![引用图][guide]\n\n[guide]: <${referenceSource}>`}
        resolveImage={resolveImage}
        imageScope="project-unicode"
      />,
    );

    await waitFor(() => {
      expect(resolveImage).toHaveBeenCalledWith(directSource);
      expect(resolveImage).toHaveBeenCalledWith(referenceSource);
    });
    expect(resolveImage.mock.calls.map(([source]) => source)).toEqual(
      expect.arrayContaining([directSource, referenceSource]),
    );
  });

  it("uses the first definition when reference image labels are duplicated", async () => {
    const firstSource = "guide-asset:projects/p/guide/first-中文.png";
    const secondSource = "guide-asset:projects/p/guide/second-中文.png";
    const resolveImage = vi.fn().mockResolvedValue({
      url: "https://cdn.example/guide.png",
      expiresAt: Date.now() + 60_000,
    });
    render(
      <MarkdownView
        content={`![引用图][guide]\n\n[guide]: <${firstSource}>\n[GUIDE]: <${secondSource}>`}
        resolveImage={resolveImage}
        imageScope="project-duplicate-reference"
      />,
    );

    await waitFor(() => expect(resolveImage).toHaveBeenCalledWith(firstSource));
    expect(resolveImage).not.toHaveBeenCalledWith(secondSource);
  });

  it("keeps linked images as ordinary links without a nested button", async () => {
    render(
      <MarkdownView content="[![外链图片](https://cdn.example/a.png)](https://example.com)" />,
    );
    const link = screen.getByRole("link", { name: "外链图片" });
    expect(link).toHaveAttribute("href", "https://example.com");
    expect(link.querySelector("button")).toBeNull();
    expect(link.querySelector("img")).toHaveAttribute("src", "https://cdn.example/a.png");
  });

  it("ignores a late guide-image result after the document scope changes", async () => {
    const first = deferred<{ url: string }>();
    const second = deferred<{ url: string }>();
    const resolveImage = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise);
    const view = render(
      <MarkdownView
        content="![旧图](guide-asset:old)"
        resolveImage={resolveImage}
        imageScope="project-old"
      />,
    );

    view.rerender(
      <MarkdownView
        content="![新图](guide-asset:new)"
        resolveImage={resolveImage}
        imageScope="project-new"
      />,
    );
    first.resolve({ url: "https://cdn.example/old.png" });
    second.resolve({ url: "https://cdn.example/new.png" });

    expect(await screen.findByAltText("新图")).toHaveAttribute(
      "src",
      "https://cdn.example/new.png",
    );
    expect(screen.queryByAltText("旧图")).not.toBeInTheDocument();
  });

  it("offers a retry and refreshes a failed guide image", async () => {
    const resolveImage = vi
      .fn()
      .mockRejectedValueOnce({ status: 404 })
      .mockResolvedValueOnce({ url: "https://cdn.example/recovered.png" });
    render(
      <MarkdownView
        content="![资源](guide-asset:missing)"
        resolveImage={resolveImage}
        imageScope="project-1"
      />,
    );

    expect(await screen.findByRole("status", { name: "图片已不存在" })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("markdown-image-retry"));
    await waitFor(() =>
      expect(resolveImage).toHaveBeenLastCalledWith("guide-asset:missing", { refresh: true }),
    );
    expect(await screen.findByAltText("资源")).toHaveAttribute(
      "src",
      "https://cdn.example/recovered.png",
    );
  });

  it("bounds automatic refreshes until the replacement image really loads", async () => {
    const resolveImage = vi
      .fn()
      .mockResolvedValueOnce({ url: "https://cdn.example/expired.png" })
      .mockResolvedValueOnce({ url: "https://cdn.example/refreshed.png" })
      .mockRejectedValueOnce({ status: 404 });
    render(
      <MarkdownView
        content="![资源](guide-asset:asset)"
        resolveImage={resolveImage}
        imageScope="project-1"
      />,
    );

    const firstImage = await screen.findByAltText("资源");
    fireEvent.error(firstImage);
    await waitFor(() => expect(resolveImage).toHaveBeenCalledTimes(2));
    const refreshedImage = await screen.findByAltText("资源");
    fireEvent.error(refreshedImage);
    expect(await screen.findByRole("status", { name: "图片暂时不可用" })).toBeInTheDocument();
    expect(resolveImage).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByTestId("markdown-image-retry"));
    await waitFor(() => expect(resolveImage).toHaveBeenCalledTimes(3));
    expect(await screen.findByRole("status", { name: "图片已不存在" })).toBeInTheDocument();
    expect(resolveImage).toHaveBeenCalledTimes(3);
  });

  it("does not resolve the same image again when only Markdown content changes", async () => {
    const resolveImage = vi.fn().mockResolvedValue({ url: "https://cdn.example/stable.png" });
    const view = render(
      <MarkdownView
        content="![资源](guide-asset:asset)"
        resolveImage={resolveImage}
        imageScope="project-1"
      />,
    );
    expect(await screen.findByAltText("资源")).toHaveAttribute(
      "src",
      "https://cdn.example/stable.png",
    );
    expect(resolveImage).toHaveBeenCalledTimes(1);

    view.rerender(
      <MarkdownView
        content="# 更新后的文字\n\n![资源](guide-asset:asset)"
        resolveImage={resolveImage}
        imageScope="project-1"
      />,
    );
    expect(await screen.findByAltText("资源")).toHaveAttribute(
      "src",
      "https://cdn.example/stable.png",
    );
    expect(resolveImage).toHaveBeenCalledTimes(1);
  });
});
