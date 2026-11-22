import { test as base, type Browser, type BrowserServer } from "@playwright/test";

export async function closeRasterBrowser(server: BrowserServer): Promise<void> {
  const child = server.process();
  const alreadyExited = child.exitCode !== null || child.signalCode !== null;
  const outcome = alreadyExited
    ? Promise.resolve({ code: child.exitCode, signal: child.signalCode })
    : new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve) => {
        child.once("exit", (code, signal) => resolve({ code, signal }));
      });
  await server.close();
  const { code, signal } = await outcome;
  if (alreadyExited || code !== 0 || signal !== null) {
    throw new Error(
      `raster_browser_exit_unsettled: ${JSON.stringify({ pid: child.pid, code, signal, alreadyExited })}`,
    );
  }
}

export const test = base.extend({
  browser: [
    async ({ playwright, browserName, launchOptions, headless, channel, connectOptions }, use) => {
      if (connectOptions) {
        throw new Error("Raster qualification requires its own local browser process");
      }
      const server = await playwright[browserName].launchServer({
        handleSIGINT: false,
        ...launchOptions,
        headless,
        channel,
        host: "127.0.0.1",
      });
      let browser: Browser | undefined;
      const failures: unknown[] = [];
      try {
        browser = await playwright[browserName].connect(server.wsEndpoint());
        await use(browser);
      } catch (error) {
        failures.push(error);
      }
      try {
        await browser?.close();
      } catch (error) {
        failures.push(error);
      }
      try {
        await closeRasterBrowser(server);
      } catch (error) {
        failures.push(error);
      }
      if (failures.length === 1) throw failures[0];
      if (failures.length > 1) {
        throw new AggregateError(failures, "Raster browser consumer and shutdown failed");
      }
    },
    { scope: "worker", timeout: 0 },
  ],
});
