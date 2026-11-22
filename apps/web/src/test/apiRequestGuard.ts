import { AsyncLocalStorage } from "node:async_hooks";
import type { SetupServer } from "msw/node";
import { vi, type File, type Suite, type Task, type TaskResultPack } from "vitest";
import { VitestTestRunner } from "vitest/runners";
import { getFn, getHooks, setHooks } from "vitest/suite";

const PRODUCT_API = /^\/api\/v\d+(?:\/|$)/;

type RunInScope = <T>(callback: () => T) => T;

export default class ApiRequestGuardRunner extends VitestTestRunner {
  readonly #owner = new AsyncLocalStorage<Task>();
  readonly #scopes = new WeakMap<Task, RunInScope>();
  readonly #misses = new Map<Task, Set<string>>();
  #server?: SetupServer;

  async onBeforeRunFiles([file]: File[] = []) {
    super.onBeforeRunFiles();
    if (!file) return;
    const { server } = await import("../mocks/server");
    this.#server = server;
    server.listen({
      onUnhandledRequest: (request) => {
        const { pathname, search } = new URL(request.url);
        if (!PRODUCT_API.test(pathname)) return;

        const owner = this.#owner.getStore() ?? file;
        let misses = this.#misses.get(owner);
        if (!misses) this.#misses.set(owner, (misses = new Set()));
        misses.add(`${request.method} ${pathname}${search}`);
        throw Response.error();
      },
    });
  }

  onCollected(files: File[]) {
    for (const file of files) this.#wrapHooks(file);
  }

  #wrapHooks(suite: Suite) {
    const hooks = getHooks(suite);
    if (!hooks) return;
    setHooks(suite, {
      beforeAll: hooks.beforeAll.map((hook) => this.#wrapHook(hook, () => suite)),
      afterAll: hooks.afterAll.map((hook) => this.#wrapHook(hook, () => suite)),
      beforeEach: hooks.beforeEach.map((hook) => this.#wrapHook(hook, (context) => context.task)),
      afterEach: hooks.afterEach.map((hook) => this.#wrapHook(hook, (context) => context.task)),
    });
    for (const task of suite.tasks) {
      if (task.type === "suite") this.#wrapHooks(task);
    }
  }

  #wrapHook<Args extends unknown[]>(
    hook: (...args: Args) => unknown,
    getOwner: (...args: Args) => Task,
  ) {
    return (...args: Args) => {
      const run = this.#scope(getOwner(...args));
      return run(async () => {
        const cleanup = await hook(...args);
        return typeof cleanup === "function" ? () => run(() => cleanup()) : cleanup;
      });
    };
  }

  #scope(task: Task): RunInScope {
    let run = this.#scopes.get(task);
    if (!run) {
      const create = this.#server!.boundary(() =>
        this.#owner.run(task, () => AsyncLocalStorage.snapshot()),
      );
      run = task === task.file ? create() : this.#scope(task.suite ?? task.file)(create);
      this.#scopes.set(task, run);
    }
    return run;
  }

  onBeforeTryTask(test: Task) {
    this.#restoreTransportDoubles();
    super.onBeforeTryTask(test);
    this.#scopes.delete(test);
  }

  async runTask(test: Task) {
    if (test.type === "suite") throw new Error("Expected a Vitest test task");
    const run = this.#scope(test);
    try {
      await run(getFn(test));
    } finally {
      for (const callbacks of [test.onFinished, test.onFailed]) {
        callbacks?.forEach((callback, index) => {
          callbacks[index] = (...args) => run(() => callback(...args));
        });
      }
    }
  }

  #restoreTransportDoubles() {
    vi.unstubAllGlobals();
    for (const transport of [globalThis.fetch, globalThis.XMLHttpRequest]) {
      if (vi.isMockFunction(transport)) transport.mockRestore();
    }
  }

  declare onTaskUpdate: (packs: TaskResultPack[]) => Promise<void>;

  async onAfterRunFiles() {
    try {
      const updates = new Map<string, TaskResultPack>();
      for (const [task, misses] of this.#misses) {
        const name = "UnhandledProductApiRequest";
        const message = `未声明 MSW handler 的产品 API 请求：\n${[...misses].join("\n")}`;
        task.result ??= { state: "fail" };
        (task.result.errors ??= []).push({ name, nameStr: name, message });
        for (let owner: Task = task; ; owner = owner.suite ?? owner.file) {
          if (owner.result) {
            owner.result.state = "fail";
            updates.set(owner.id, [owner.id, owner.result, owner.meta]);
          }
          if (owner === owner.file) break;
        }
      }
      this.#misses.clear();
      if (updates.size) await this.onTaskUpdate([...updates.values()]);
    } finally {
      this.#restoreTransportDoubles();
      this.#server?.close();
      this.#server = undefined;
      super.onAfterRunFiles();
    }
  }
}
