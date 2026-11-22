import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { renderSettings } from "./generate-settings.mjs";

const stateDir = new URL("../../apps/web/src/pages/Workbench/state/", import.meta.url);
const fields = readFileSync(new URL("workbenchSettingsFields.ts", stateDir), "utf8");
const defaults = readFileSync(new URL("workbenchLocalDefaults.ts", stateDir), "utf8");
const auth = readFileSync(new URL("../../apps/web/src/api/auth.ts", import.meta.url), "utf8");
const newField = `{
  key: "experiment.newToggle", category: "experiment", section: "experiments",
  storage: "local", label: "新开关", control: { type: "toggle" },
  read: () => { throw new Error("生成器不得执行浏览器逻辑"); }, write: () => {},
},`;
const withNewField = fields.replace(/(WORKBENCH_SETTING_FIELDS[^=]*=\s*\[)/, `$1\n${newField}`);
const withDefault = defaults.replace("} as const", '  "experiment.newToggle": true,\n} as const');
const webcodecsDefault = /("experiment\.webcodecs": )\w+/;
const referenceDefault = /("experiment\.videoReferencePredict": )"[^"]+"/;

test("生成新增本机开关，并显示本机单选项的默认标签", () => {
  const { content } = renderSettings(
    withNewField,
    auth,
    withDefault.replace(referenceDefault, '$1"off"'),
  );
  assert.match(content, /\| 新开关 \|  \| 开启 \|/);
  assert.match(content, /\| 参考框运动预测 \|[^\n]+\| 关\(最近关键帧\) \|/);
});

test("本机字段缺少默认值、默认值没有对应字段或选择值未知时拒绝生成", () => {
  assert.throws(
    () => renderSettings(withNewField, auth, defaults),
    /experiment.newToggle 缺少本机默认值/,
  );
  assert.throws(
    () => renderSettings(fields, auth, withDefault),
    /experiment.newToggle 是未知本机默认值/,
  );
  assert.throws(
    () => renderSettings(fields, auth, defaults.replace(referenceDefault, '$1"unsupported"')),
    /videoReferencePredict 的本机默认值不匹配控件/,
  );
});

test("无法静态识别的运行时字段不能被手册静默省略", () => {
  const source = withNewField.replace('label: "新开关"', "label: browserLabel()");
  assert.throws(
    () => renderSettings(source, auth, withDefault),
    /experiment.newToggle 缺少有效字段定义/,
  );
  const unknownField = fields.replace('key: "image.fadedOpacity"', 'key: "image.unknown"');
  assert.throws(
    () => renderSettings(unknownField, auth, defaults),
    /image.unknown 缺少可读取的默认值/,
  );
});

test("默认值修改后 --check 报告漂移，重新生成后通过", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "yoao-settings-codegen-"));
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  const dst = join(dir, "settings.generated.md");
  const baseline = defaults
    .replace(webcodecsDefault, "$1true")
    .replace(referenceDefault, '$1"off"');
  const { content: previous } = renderSettings(fields, auth, baseline);
  writeFileSync(dst, previous);
  const { content } = renderSettings(
    fields,
    auth,
    baseline.replace(webcodecsDefault, "$1false").replace(referenceDefault, '$1"linear"'),
  );
  assert.match(content, /\| WebCodecs 精确解码 \|[^\n]+\| 关闭 \|/);
  assert.match(content, /\| 参考框运动预测 \|[^\n]+\| 恒速外推\(前两关键帧\) \|/);
  const script = `
    import { readFileSync } from "node:fs";
    import { emitGenerated } from ${JSON.stringify(new URL("./_emit.mjs", import.meta.url).href)};
    emitGenerated({ dst: process.argv[1], content: readFileSync(0, "utf8"), label: "generate-settings" });
  `;
  const emit = (...args) =>
    spawnSync(process.execPath, ["--input-type=module", "--eval", script, "--", dst, ...args], {
      input: content,
      encoding: "utf8",
    });
  const drift = emit("--check");
  assert.equal(drift.status, 1, drift.stderr);
  assert.match(drift.stderr, /与源不一致/);
  assert.equal(readFileSync(dst, "utf8"), previous);
  const regenerate = emit();
  assert.equal(regenerate.status, 0, regenerate.stderr);
  const fresh = emit("--check");
  assert.equal(fresh.status, 0, fresh.stderr);
});
