import { afterEach, describe, expect, it, vi } from "vitest";
import { randomId } from "./id";

describe("randomId", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("在远程 HTTP 缺少 randomUUID 时仍生成可用 UUID", () => {
    vi.stubGlobal("crypto", {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.fill(7);
        return bytes;
      },
    });

    expect(randomId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });

  it.each([{}, undefined])("keeps UUID v4 format without Web Crypto (%s)", (crypto) => {
    vi.stubGlobal("crypto", crypto);
    expect(randomId()).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
  });
});
