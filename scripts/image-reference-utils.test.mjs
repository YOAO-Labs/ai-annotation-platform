import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";
import {
  extractImageReferences,
  resolveLocalMediaPath,
  withoutMarkdownCode,
} from "./image-reference-utils.mjs";

const docsRoot = "/repo/docs-site";
const mdPath = "/repo/docs-site/user-guide/projects/index.md";

test("extracts Markdown images and static HTML img sources", () => {
  assert.deepEqual(
    extractImageReferences(`
![A](../images/a.png)
![With a title](<../images/a second.png> "Title")
<img src="../images/b.png" />
<img :src="dynamic" />
`),
    ["../images/a.png", "../images/a second.png", "../images/b.png"],
  );
});

test("does not treat examples in fences, inline code or comments as references", () => {
  const markdown = [
    "````markdown",
    "![Example](missing.png)",
    "```",
    '<img src="also-missing.png" />',
    "`````",
    "`![Inline example](inline.png)`",
    "<!-- ![Comment](comment.png) -->",
    "![Published](published.png)",
  ].join("\n");
  assert.deepEqual(extractImageReferences(withoutMarkdownCode(markdown)), ["published.png"]);
});

test("resolves local image paths without metadata and skips external sources", () => {
  const resolve = (src) => resolveLocalMediaPath(src, mdPath, docsRoot);
  assert.equal(
    resolve("../images/my%20capture.webp?revision=2#example"),
    path.join(docsRoot, "user-guide/images/my capture.webp"),
  );
  assert.equal(
    resolve("/user-guide/images/capture.webp"),
    path.join(docsRoot, "user-guide/images/capture.webp"),
  );
  assert.equal(resolve("//example.com/capture.webp"), null);
  assert.equal(resolve("https://example.com/capture.webp"), null);
  assert.equal(resolve("data:image/png;base64,AAAA"), null);
});
