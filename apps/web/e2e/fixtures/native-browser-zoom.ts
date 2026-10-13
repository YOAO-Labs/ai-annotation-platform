import { chromium, type BrowserContext, type Page } from "@playwright/test";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

/**
 * Chromium creates its POSIX singleton socket at
 * `<TMPDIR>/org.chromium.Chromium.XXXXXX/SingletonSocket`, bounded by
 * `sockaddr_un.sun_path` (108 bytes on this Linux host). The long worktree e2e
 * `TMPDIR` overruns it, so the full Chromium build the MV3 zoom extension
 * requires aborts with SIGTRAP before any page loads: measured here, a 77-byte
 * TMPDIR produces a 122-byte socket path and fails, while an owned 39-byte
 * `/tmp` TMPDIR produces an 84-byte path and passes with the same arguments.
 * Give the browser process an owned short temp directory; `close` removes it on
 * launch failure and normal close. Windows uses named pipes without this limit
 * and keeps the inherited environment.
 */
async function makeBrowserTempDirectory(): Promise<string | undefined> {
  if (process.platform === "win32") return undefined;
  return mkdtemp(join("/tmp", "aap-native-browser-zoom-tmp-"));
}

interface ZoomExtensionScope {
  chrome: {
    tabs: {
      query(options: Record<string, never>): Promise<Array<{ id?: number }>>;
      setZoomSettings(
        tabId: number,
        settings: { mode: "automatic"; scope: "per-tab" },
      ): Promise<void>;
      setZoom(tabId: number, factor: number): Promise<void>;
      getZoom(tabId: number): Promise<number>;
    };
  };
}

export interface NativeBrowserZoom {
  context: BrowserContext;
  page: Page;
  /** Call after navigation; Chromium resets per-tab zoom on document navigation. */
  setZoom(factor: number): Promise<void>;
  getZoom(): Promise<number>;
  /** Always call in finally, including when a test assertion fails. */
  close(): Promise<void>;
}

/**
 * Launch a disposable Chromium profile with an MV3 extension that uses the native
 * tabs zoom API. No CSS zoom, device emulation, user profile, or CDP commands.
 *
 * windowSize is the outer browser window in device-independent pixels. Browser
 * chrome can reduce the content height. Keep viewport null and do not call
 * page.setViewportSize(), which would hide native zoom's CSS viewport changes.
 * See https://playwright.dev/docs/chrome-extensions and
 * https://developer.chrome.com/docs/extensions/reference/api/tabs#method-setZoom.
 */
export async function launchNativeBrowserZoom(
  options: {
    baseURL?: string;
    headless?: boolean;
    windowSize?: { width: number; height: number };
  } = {},
): Promise<NativeBrowserZoom> {
  const windowSize = options.windowSize ?? { width: 1280, height: 900 };
  if (
    !Number.isSafeInteger(windowSize.width) ||
    !Number.isSafeInteger(windowSize.height) ||
    windowSize.width <= 0 ||
    windowSize.height <= 0
  ) {
    throw new Error("Native browser zoom requires positive integer window dimensions");
  }

  const directory = await mkdtemp(join(tmpdir(), "aap-native-browser-zoom-"));
  const browserTempDirectory = await makeBrowserTempDirectory();
  const browserEnv = browserTempDirectory
    ? {
        ...process.env,
        TMPDIR: browserTempDirectory,
        TMP: browserTempDirectory,
        TEMP: browserTempDirectory,
      }
    : process.env;
  let context: BrowserContext | undefined;
  let closing: Promise<void> | undefined;
  const close = (): Promise<void> => {
    closing ??= (async () => {
      try {
        await context?.close();
      } finally {
        await rm(directory, { recursive: true, force: true });
        if (browserTempDirectory) {
          await rm(browserTempDirectory, { recursive: true, force: true });
        }
      }
    })();
    return closing;
  };

  try {
    const extensionPath = join(directory, "extension");
    await mkdir(extensionPath);
    await writeFile(
      join(extensionPath, "manifest.json"),
      JSON.stringify({
        manifest_version: 3,
        name: "Annotation native zoom test helper",
        version: "1.0.0",
        background: { service_worker: "background.js" },
      }),
    );
    await writeFile(
      join(extensionPath, "background.js"),
      "chrome.runtime.onInstalled.addListener(() => {});\n",
    );

    context = await chromium.launchPersistentContext(join(directory, "profile"), {
      channel: "chromium",
      headless: options.headless ?? true,
      baseURL: options.baseURL,
      viewport: null,
      // Clear a Playwright Test project's inherited device preset for native zoom.
      deviceScaleFactor: undefined,
      env: browserEnv,
      args: [
        `--window-size=${windowSize.width},${windowSize.height}`,
        `--disable-extensions-except=${extensionPath}`,
        `--load-extension=${extensionPath}`,
      ],
    });
    const worker = context.serviceWorkers()[0] ?? (await context.waitForEvent("serviceworker"));
    const page = context.pages()[0];
    const tabId = await worker.evaluate(async () => {
      const tabs = await (globalThis as unknown as ZoomExtensionScope).chrome.tabs.query({});
      if (tabs.length !== 1 || tabs[0].id === undefined) {
        throw new Error("Native browser zoom expected one initial tab in its isolated profile");
      }
      return tabs[0].id;
    });
    if (!page) throw new Error("Native browser zoom has no initial page");

    const getZoom = () =>
      worker.evaluate(
        (id) => (globalThis as unknown as ZoomExtensionScope).chrome.tabs.getZoom(id),
        tabId,
      );

    return {
      context,
      page,
      getZoom,
      async setZoom(factor) {
        if (!Number.isFinite(factor) || factor <= 0) {
          throw new Error("Native browser zoom requires a positive finite zoom factor");
        }
        await worker.evaluate(
          async ({ id, value }) => {
            const { tabs } = (globalThis as unknown as ZoomExtensionScope).chrome;
            await tabs.setZoomSettings(id, { mode: "automatic", scope: "per-tab" });
            await tabs.setZoom(id, value);
          },
          { id: tabId, value: factor },
        );
        const actual = await getZoom();
        if (Math.abs(actual - factor) > 0.0001) {
          throw new Error(`Chromium applied zoom ${actual}, expected ${factor}`);
        }
        // Let Chromium finish applying the new layout before the caller measures it.
        await page.evaluate(
          () =>
            new Promise<void>((resolve) =>
              requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
            ),
        );
      },
      close,
    };
  } catch (error) {
    await close();
    throw error;
  }
}
