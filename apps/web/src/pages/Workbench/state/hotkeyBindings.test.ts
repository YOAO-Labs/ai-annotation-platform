import { describe, expect, it } from "vitest";
import {
  EDITABLE_SHORTCUT_COMMANDS,
  FIXED_SHORTCUTS,
  bindingToken,
  createCommandEventMatcher,
  findBindingConflicts,
  getEditableCommand,
  normalizeRecordedEvent,
  parseStoredShortcutOverrides,
  resolveEffectiveCommands,
  whenOverlaps,
  type ShortcutBinding,
} from "./hotkeyBindings";
import { HOTKEYS } from "./hotkeys";

function ev(init: Partial<KeyboardEvent> & { key?: string; code?: string }): KeyboardEvent {
  return new KeyboardEvent("keydown", init as KeyboardEventInit);
}

const bind = (key: string, ...modifiers: ("mod" | "alt" | "shift")[]): ShortcutBinding => ({
  key,
  modifiers: [...modifiers].sort(),
});

describe("快捷键注册表不变式", () => {
  it("默认组合之间不存在冲突（允许的共用必须来自互补谓词）", () => {
    const effective = resolveEffectiveCommands({ common: {}, image: {}, video: {} });
    for (const state of effective.values()) {
      expect(state.conflictsWith, `${state.id} 与默认注册表冲突`).toEqual([]);
    }
  });

  it("命令 ID 全局唯一且与参考目录一致", () => {
    const ids = EDITABLE_SHORTCUT_COMMANDS.map((c) => c.id);
    expect(new Set(ids).size).toBe(ids.length);
    const catalogIds = new Set(HOTKEYS.map((h) => h.id));
    for (const id of ids) expect(catalogIds.has(id), `${id} 应存在于 HOTKEYS 目录`).toBe(true);
  });

  it("每条命令最多一主一备两个组合", () => {
    for (const c of EDITABLE_SHORTCUT_COMMANDS) {
      expect(c.bindings.length, c.id).toBeLessThanOrEqual(2);
      expect(c.bindings.length, c.id).toBeGreaterThanOrEqual(1);
    }
  });

  it("Fixed 清单覆盖项目类别数字键与物理键位例外", () => {
    const category = FIXED_SHORTCUTS.find((f) => f.id === "fixed.category.digits");
    expect(category?.bindings.map((b) => b.key)).toEqual([
      "1",
      "2",
      "3",
      "4",
      "5",
      "6",
      "7",
      "8",
      "9",
      "0",
    ]);
    const backquote = FIXED_SHORTCUTS.find((f) => f.id === "fixed.backquoteStep");
    expect(backquote?.bindings.some((b) => b.key === "`")).toBe(true);
  });

  it("互斥谓词允许共用按键；非互斥谓词视为重叠", () => {
    expect(whenOverlaps("selection", "noSelection")).toBe(false);
    expect(whenOverlaps("videoTrack", "videoNoTrack")).toBe(false);
    expect(whenOverlaps("prediction", "noPrediction")).toBe(false);
    expect(whenOverlaps("any", "selection")).toBe(true);
    expect(whenOverlaps("any", "any")).toBe(true);
    expect(whenOverlaps("sampling", "noSelection")).toBe(true);
  });
});

describe("候选编辑冲突检查", () => {
  const effective = resolveEffectiveCommands({ common: {}, image: {}, video: {} });

  it("新组合撞上其他可编辑命令 → 拒绝并命名对方", () => {
    const conflicts = findBindingConflicts({
      commandId: "image.tool.keypoint", // F
      bindings: [bind("o")], // video.track.outside 同键但 image/video 域不相交 → 换一个真正冲突的
      effective,
    });
    expect(conflicts.filter((c) => !c.fixed)).toEqual([]);
    const overlap = findBindingConflicts({
      commandId: "image.tool.keypoint",
      bindings: [bind("b")], // 图片 box 工具
      effective,
    });
    expect(overlap.map((c) => c.targetId)).toContain("image.tool.box");
    expect(overlap[0].fixed).toBe(false);
  });

  it("与 Fixed 命令重叠 → 拒绝并给出上下文说明", () => {
    const conflicts = findBindingConflicts({
      commandId: "image.tool.keypoint",
      bindings: [bind("n")], // 智能切题（fixed.image.smartNext）
      effective,
    });
    expect(conflicts.map((c) => c.targetId)).toContain("fixed.image.smartNext");
    expect(conflicts[0].fixed).toBe(true);
    expect(conflicts[0].applies).toContain("图片");
  });

  it("common 域命令与 3D Fixed 键重叠 → 拒绝", () => {
    const conflicts = findBindingConflicts({
      commandId: "common.task.next",
      bindings: [bind("arrowright", "shift")], // 3D 跨帧延续
      effective,
    });
    expect(conflicts.map((c) => c.targetId)).toContain("fixed.threed.propagate");
  });

  it("互补谓词的共用被允许（L 锁定 vs 折线工具）", () => {
    const conflicts = findBindingConflicts({
      commandId: "image.selection.lock",
      bindings: [bind("l")],
      effective,
    });
    expect(conflicts.filter((c) => c.targetId === "image.tool.polyline")).toEqual([]);
  });

  it("与项目类别数字键冲突 → 拒绝", () => {
    const conflicts = findBindingConflicts({
      commandId: "image.tool.keypoint",
      bindings: [bind("7")],
      effective,
    });
    expect(conflicts.map((c) => c.targetId)).toContain("fixed.category.digits");
  });
});

describe("生效绑定解析", () => {
  it("缺失 = 默认；null = 恢复默认；[] = 停用；列表 = 整体替换", () => {
    const effective = resolveEffectiveCommands({
      common: {},
      image: {
        "image.tool.box": [bind("k")],
        "image.tool.select": [],
      },
      video: { "video.tool.box": null },
    });
    expect(effective.get("image.tool.box")?.bindings).toEqual([bind("k")]);
    expect(effective.get("image.tool.box")?.customized).toBe(true);
    expect(effective.get("image.tool.select")?.disabled).toBe(true);
    expect(effective.get("video.tool.box")?.customized).toBe(false);
    expect(effective.get("video.tool.box")?.bindings.map((b) => b.key)).toEqual(["b", "1"]);
  });

  it("互相争用的覆盖成对停用（加载期复查；运行期不执行）", () => {
    const effective = resolveEffectiveCommands({
      common: {},
      image: { "image.tool.keypoint": [bind("b")] },
      video: {},
    });
    expect(effective.get("image.tool.keypoint")?.conflictsWith).toContain("image.tool.box");
    expect(effective.get("image.tool.box")?.conflictsWith).toContain("image.tool.keypoint");
    // 运行期匹配对冲突命令一律不命中（执行方吞掉事件，入口保留在指针操作）。
    const matcher = createCommandEventMatcher(ev({ key: "b" }), effective, "image");
    expect(matcher.match("image.tool.box")).toBe(false);
    expect(matcher.match("image.tool.keypoint")).toBe(false);
    // 冲突仍要被识别并上报：两条覆盖互撞时不能静默丢弃事件。
    expect(matcher.conflict).toBe(true);
  });
});

describe("运行期匹配", () => {
  const effective = resolveEffectiveCommands({ common: {}, image: {}, video: {} });

  it("修饰键要求完全一致", () => {
    const shiftB = createCommandEventMatcher(ev({ key: "b", shiftKey: true }), effective, "image");
    expect(shiftB.match("image.tool.box")).toBe(false);
    const modShiftZ = createCommandEventMatcher(
      ev({ key: "z", ctrlKey: true, shiftKey: true }),
      effective,
      "image",
    );
    expect(modShiftZ.match("image.undo")).toBe(false);
  });

  it("common 命令在两个工作台都命中；域之间不串", () => {
    expect(
      createCommandEventMatcher(ev({ key: "arrowright", ctrlKey: true }), effective, "image").match(
        "common.task.next",
      ),
    ).toBe(true);
    expect(
      createCommandEventMatcher(ev({ key: "arrowright", ctrlKey: true }), effective, "video").match(
        "common.task.next",
      ),
    ).toBe(true);
    expect(
      createCommandEventMatcher(ev({ key: "arrowright", ctrlKey: true }), effective, "video").match(
        "video.frame.next",
      ),
    ).toBe(false);
  });

  it("备用组合命中同一命令", () => {
    expect(
      createCommandEventMatcher(ev({ key: "1", altKey: true }), effective, "image").match(
        "image.tool.box",
      ),
    ).toBe(true);
  });
});

describe("录制规范化", () => {
  it("字符键转小写 + 显式修饰键排序", () => {
    expect(normalizeRecordedEvent(ev({ key: "B", shiftKey: true })).binding).toEqual({
      key: "b",
      modifiers: ["shift"],
    });
    expect(normalizeRecordedEvent(ev({ key: "ArrowRight", ctrlKey: true })).binding).toEqual({
      key: "arrowright",
      modifiers: ["mod"],
    });
    expect(bindingToken({ key: "z", modifiers: ["shift", "mod"] })).toBe("mod+shift+z");
  });

  it("拒绝仅修饰键、物理例外与浏览器保留组合", () => {
    expect(normalizeRecordedEvent(ev({ key: "Shift" })).reason).toBe("modifier-only");
    expect(normalizeRecordedEvent(ev({ key: "`" })).reason).toBe("physical");
    expect(normalizeRecordedEvent(ev({ key: "t", ctrlKey: true })).reason).toBe("browser");
    expect(normalizeRecordedEvent(ev({ key: "Escape" })).reason).toBe("physical");
    expect(normalizeRecordedEvent(ev({ key: "q", ctrlKey: true, shiftKey: true })).reason).toBe(
      "browser",
    );
  });

  it("拒绝无法持久化的命名键，与存量解析保持一致", () => {
    // 录制若接受 F13 / Dead，写入会被保存却在重新解析时判为非法并回退默认。
    expect(normalizeRecordedEvent(ev({ key: "F13" })).reason).toBe("unsupported");
    expect(normalizeRecordedEvent(ev({ key: "Dead" })).reason).toBe("unsupported");
    expect(normalizeRecordedEvent(ev({ key: "F5" })).binding).toEqual({
      key: "f5",
      modifiers: [],
    });
  });
});

describe("存量偏好宽容解析", () => {
  it("接受 schemaVersion 1 的合法覆盖", () => {
    const parsed = parseStoredShortcutOverrides({
      schemaVersion: 1,
      image: { "image.tool.box": [{ key: "k", modifiers: [] }] },
      video: { "video.tool.mask": null },
    });
    expect(parsed.opaque).toBe(false);
    expect(parsed.issues).toEqual([]);
    expect(parsed.overrides.image["image.tool.box"]).toEqual([{ key: "k", modifiers: [] }]);
    expect(parsed.overrides.video["video.tool.mask"]).toBeNull();
  });

  it("未知命令 / 非法组合排除出执行并记入 issues，保留原值语义", () => {
    const parsed = parseStoredShortcutOverrides({
      schemaVersion: 1,
      image: {
        "legacy.command": [{ key: "k" }],
        "image.tool.box": [{ key: "CapsLock", modifiers: [] }],
      },
    });
    expect(parsed.opaque).toBe(false);
    expect(parsed.issues.map((i) => i.reason)).toEqual(["unknown-command", "invalid-binding"]);
    expect(parsed.overrides.image["image.tool.box"]).toBeUndefined();
  });

  it("未知命令的 null 视为已清除，不再报 unknown-command", () => {
    const parsed = parseStoredShortcutOverrides({
      schemaVersion: 1,
      image: { "legacy.command": null, "image.tool.box": null },
    });
    expect(parsed.issues).toEqual([]);
    expect(parsed.overrides.image["legacy.command"]).toBeUndefined();
    expect(parsed.overrides.image["image.tool.box"]).toBeNull();
  });

  it("缺失 / 更新版本 / 非对象子树整体视为 opaque", () => {
    expect(parseStoredShortcutOverrides(undefined).opaque).toBe(false);
    expect(parseStoredShortcutOverrides({ schemaVersion: 2 }).opaque).toBe(true);
    expect(parseStoredShortcutOverrides("oops").opaque).toBe(true);
    expect(parseStoredShortcutOverrides({ schemaVersion: 1, image: 5 }).opaque).toBe(true);
  });

  it("组合列表长度超限视为非法", () => {
    const parsed = parseStoredShortcutOverrides({
      schemaVersion: 1,
      image: { "image.tool.box": [{ key: "b" }, { key: "1", modifiers: ["alt"] }, { key: "x" }] },
    });
    expect(parsed.issues.map((i) => i.commandId)).toEqual(["image.tool.box"]);
  });
});

describe("目录一致性", () => {
  it("每个可编辑命令都能在 HOTKEYS 中按 id 找到并带默认组合展示", () => {
    for (const c of EDITABLE_SHORTCUT_COMMANDS) {
      const rows = HOTKEYS.filter((h) => h.id === c.id || h.id === `${c.id}.alt`);
      expect(rows.length, c.id).toBeGreaterThan(0);
    }
  });

  it("getEditableCommand 覆盖注册表中的全部命令", () => {
    for (const c of EDITABLE_SHORTCUT_COMMANDS) {
      expect(getEditableCommand(c.id)?.id).toBe(c.id);
    }
    expect(getEditableCommand("nope.nope")).toBeUndefined();
  });
});

describe("固定监听与自定义绑定的回归", () => {
  const effective = resolveEffectiveCommands({ common: {}, image: {}, video: {} });

  it.each([
    ["image.tool.select", bind("a", "shift"), "fixed.review.approve"],
    ["common.task.next", bind("p", "shift"), "fixed.threed.gizmo"],
    ["common.task.next", bind("d", "mod", "shift"), "fixed.threed.clipboardHistory"],
    ["video.tool.select", bind("r", "shift"), "fixed.review.reject"],
    ["video.tool.select", bind("t", "shift"), "fixed.video.propagateTrack"],
    ["video.tool.select", bind("d", "shift"), "fixed.video.aiDecide"],
    ["video.tool.select", bind("pagedown", "mod"), "fixed.video.chapter"],
    ["image.tool.select", bind("arrowup", "shift"), "fixed.image.nudge"],
    ["video.tool.select", bind("l", "shift"), "fixed.video.jogForward"],
    ["video.tool.select", bind("home"), "fixed.video.keyframeEdges"],
    ["video.tool.select", bind("end", "shift"), "fixed.video.keyframeEdges"],
    ["image.tool.select", bind("r"), "fixed.image.samRefine"],
    ["image.tool.select", bind("b", "shift"), "fixed.image.maskLocal"],
    ["video.tool.select", bind("e", "shift"), "fixed.video.maskLocal"],
    ["image.tool.select", bind("b", "mod"), "fixed.image.maskLocal"],
  ])("%s 不能占用真实固定监听 %s", (commandId, binding, fixedId) => {
    expect(
      findBindingConflicts({ commandId, bindings: [binding], effective }).map(
        (conflict) => conflict.targetId,
      ),
    ).toContain(fixedId);
  });

  it.each(EDITABLE_SHORTCUT_COMMANDS)("$id 的默认主键加空闲备用键可保存", (def) => {
    expect(
      findBindingConflicts({
        commandId: def.id,
        bindings: [def.bindings[0], bind("x")],
        effective,
      }),
    ).toEqual([]);
  });

  it("固定选择态微调与无选中态工具键互斥，选中态命令仍不能占用", () => {
    expect(
      findBindingConflicts({
        commandId: "image.tool.polyline",
        bindings: [bind("arrowup")],
        effective,
      }),
    ).toEqual([]);
    expect(
      findBindingConflicts({
        commandId: "image.selection.lock",
        bindings: [bind("arrowup")],
        effective,
      }).map((conflict) => conflict.targetId),
    ).toContain("fixed.image.nudge");
  });

  it("存量审核危险键排除出可执行命令", () => {
    const effective = resolveEffectiveCommands({
      common: {},
      video: {},
      image: { "image.tool.select": [bind("a", "shift")] },
    });
    expect(effective.get("image.tool.select")?.conflictsWith).toContain("fixed.review.approve");
    expect(
      createCommandEventMatcher(ev({ key: "A", shiftKey: true }), effective, "image").match(
        "image.tool.select",
      ),
    ).toBe(false);
  });

  it("3D 只匹配 common 域", () => {
    const effective = resolveEffectiveCommands({
      common: { "common.task.next": [bind("x")] },
      image: { "image.selection.hide": [bind("i")] },
      video: {},
    });
    expect(
      createCommandEventMatcher(ev({ key: "i" }), effective, "threed").match(
        "image.selection.hide",
      ),
    ).toBe(false);
    expect(
      createCommandEventMatcher(ev({ key: "x" }), effective, "threed").match("common.task.next"),
    ).toBe(true);
  });
});
it("rejects the browser devtools key without modifiers", () => {
  expect(normalizeRecordedEvent(new KeyboardEvent("keydown", { key: "F12" })).reason).toBe(
    "browser",
  );
});
