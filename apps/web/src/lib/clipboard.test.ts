import { afterEach, describe, expect, it, vi } from "vitest";

import { copyToClipboard } from "./clipboard";

afterEach(() => vi.unstubAllGlobals());

describe("copyToClipboard", () => {
  it("writes the complete original value through the Clipboard API", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const value = `sha256:${"ab".repeat(32)}`;
    expect(await copyToClipboard(value)).toBe(true);
    expect(writeText).toHaveBeenCalledWith(value);
    expect(document.querySelector("textarea")).toBeNull();
  });

  it.each(["unavailable", "denied"])("uses the fallback when the API is %s", async (reason) => {
    vi.stubGlobal(
      "navigator",
      reason === "denied"
        ? { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } }
        : {},
    );
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    const original = "ea4b2a65-a050-42f3-a652-68d1c67368df";
    const execCommand = vi.fn(() => {
      const textarea = document.querySelector("textarea")!;
      expect(textarea.value).toBe(original);
      expect(textarea.selectionEnd).toBe(original.length);
      return true;
    });
    vi.stubGlobal("document", Object.assign(document, { execCommand }));
    try {
      expect(await copyToClipboard(original)).toBe(true);
      expect(execCommand).toHaveBeenCalledWith("copy");
      expect(document.querySelector("textarea")).toBeNull();
      expect(document.activeElement).toBe(trigger);
    } finally {
      trigger.remove();
      delete (document as Partial<Document>).execCommand;
    }
  });

  it.each([false, "throw"])(
    "returns failure and cleans up when fallback returns %s",
    async (result) => {
      vi.stubGlobal("navigator", {});
      const previous = document.execCommand;
      document.execCommand = vi.fn(() => {
        if (result === "throw") throw new Error("copy unavailable");
        return false;
      });
      try {
        expect(await copyToClipboard("original")).toBe(false);
        expect(document.querySelector("textarea")).toBeNull();
      } finally {
        document.execCommand = previous;
      }
    },
  );

  it.each(["dialog", "alertdialog"])("keeps the fallback inside the active %s", async (role) => {
    vi.stubGlobal("navigator", {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });
    const modal = document.createElement("div");
    modal.setAttribute("role", role);
    const trigger = document.createElement("button");
    modal.appendChild(trigger);
    document.body.appendChild(modal);
    trigger.focus();
    const previous = document.execCommand;
    document.execCommand = vi.fn(() => {
      const textarea = modal.querySelector("textarea");
      expect(textarea).not.toBeNull();
      expect(textarea?.value).toBe("historical-hash");
      return true;
    });
    try {
      expect(await copyToClipboard("historical-hash")).toBe(true);
      expect(document.activeElement).toBe(trigger);
      expect(modal.querySelector("textarea")).toBeNull();
    } finally {
      modal.remove();
      document.execCommand = previous;
    }
  });
});
