import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  fsyncSync,
  lstatSync,
  openSync,
  readFileSync,
  realpathSync,
  writeFileSync,
  readdirSync,
} from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { isDeepStrictEqual } from "node:util";
import {
  acknowledgeFixtureOperation,
  closeFixtureTransport,
  postFixture,
  prepareFixtureSuite,
  prepareScreenshotNative,
  recoverFixtureOperation,
  shutdownFixtureTransport,
} from "../helpers/seed-owner-transport.mjs";

const canonicalUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function captureDirectory(runId, { allowUnbornRoot = false } = {}) {
  if (!canonicalUuid.test(runId) || !["test", "e2e"].includes(process.env.YOAO_WORKTREE_MODE)) {
    throw new Error("Screenshot capture requires an exact isolated run identity");
  }
  const root = realpathSync(process.env.AAP_WORKTREE_RESOURCE_ROOT ?? "");
  const directory = path.join(
    root,
    ".worktree",
    process.env.YOAO_WORKTREE_MODE,
    "fixture-authority",
    `suite-${runId.replaceAll("-", "")}`,
  );
  for (const current of [path.dirname(directory), directory]) {
    const info = lstatSync(current, { throwIfNoEntry: false });
    if (!info && allowUnbornRoot && current === directory) continue;
    if (
      !info ||
      !info.isDirectory() ||
      info.uid !== process.getuid() ||
      (info.mode & 0o777) !== 0o700
    ) {
      throw new Error("Screenshot capture directory is not private");
    }
  }
  return directory;
}

function privateRecord(filename) {
  const fd = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(fd);
    const current = lstatSync(filename);
    if (
      !info.isFile() ||
      info.uid !== process.getuid() ||
      (info.mode & 0o777) !== 0o600 ||
      info.nlink !== 1 ||
      info.dev !== current.dev ||
      info.ino !== current.ino
    ) {
      throw new Error("Screenshot capture record identity changed");
    }
    return JSON.parse(readFileSync(fd, "utf8"));
  } finally {
    closeSync(fd);
  }
}

export async function prepareScreenshotCapture(runId, apiPort) {
  await prepareFixtureSuite(runId, "screenshot");
  const directory = path.dirname(captureDirectory(runId, { allowUnbornRoot: true }));
  const filename = path.join(directory, `screenshot-capture-${runId.replaceAll("-", "")}.json`);
  if (lstatSync(filename, { throwIfNoEntry: false })) {
    throw new Error("Screenshot capture already has a preparation operation");
  }
  const record = {
    schema: "yoao-screenshot-capture/v1",
    run_id: runId,
    operation_id: randomUUID(),
    cleanup_operation_id: randomUUID(),
    api_origin: `http://127.0.0.1:${apiPort}`,
  };
  const fd = openSync(
    filename,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
    0o600,
  );
  try {
    writeFileSync(fd, JSON.stringify(record));
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
  const directoryFd = openSync(
    directory,
    constants.O_RDONLY | constants.O_DIRECTORY | constants.O_NOFOLLOW,
  );
  try {
    fsyncSync(directoryFd);
  } finally {
    closeSync(directoryFd);
  }
  await prepareScreenshotNative(runId, apiPort, record.operation_id);
}

export async function seedScreenshotCapture(runId, apiOrigin, arguments_, signal) {
  const record = privateRecord(
    path.join(
      path.dirname(captureDirectory(runId)),
      `screenshot-capture-${runId.replaceAll("-", "")}.json`,
    ),
  );
  if (
    record.schema !== "yoao-screenshot-capture/v1" ||
    record.run_id !== runId ||
    record.api_origin !== apiOrigin ||
    !canonicalUuid.test(record.operation_id)
  ) {
    throw new Error("Screenshot capture preparation identity changed");
  }
  const deadline = performance.now() + 120_000;
  if (signal?.aborted) return;
  let response = await recoverFixtureOperation(record.operation_id);
  while (response.state === "pending" && performance.now() < deadline) {
    if (signal?.aborted) return;
    await delay(100);
    if (signal?.aborted) return;
    response = await recoverFixtureOperation(record.operation_id);
  }
  if (
    response.status !== 200 ||
    response.owner_error ||
    response.journal_error ||
    response.disposal_error
  ) {
    throw new Error("Screenshot capture preparation has no completed owner response");
  }
  const binding = JSON.parse(response.body);
  const origin = new URL(binding.origin);
  const original = binding.original_read;
  if (
    Object.keys(binding).sort().join(",") !==
      "admission_id,api_origin,control_dsn_file,origin,original_read,token" ||
    !canonicalUuid.test(binding.admission_id) ||
    binding.api_origin !== apiOrigin ||
    origin.protocol !== "http:" ||
    origin.hostname !== "127.0.0.1" ||
    !origin.port ||
    origin.username ||
    origin.password ||
    origin.pathname !== "/" ||
    origin.search ||
    origin.hash ||
    typeof binding.token !== "string" ||
    !/^[A-Za-z0-9_-]{43}$/.test(binding.token) ||
    typeof binding.control_dsn_file !== "string" ||
    !path.isAbsolute(binding.control_dsn_file) ||
    !original ||
    typeof original !== "object" ||
    Array.isArray(original) ||
    Object.keys(original).sort().join(",") !== "access_key,bucket,endpoint,secret_key" ||
    Object.values(original).some((value) => typeof value !== "string" || !value)
  ) {
    throw new Error("Screenshot capture seed binding changed");
  }
  await acknowledgeFixtureOperation(record.operation_id);
  if (signal?.aborted) return;
  const environment = Object.fromEntries(
    Object.entries(process.env).filter(
      ([key]) =>
        !/^(?:AWS_|MC_HOST_|AAP_PRIVATE_FIXTURE_|AAP_ASSETS_ADDITIONAL_ORIGINAL_|ASSETS_MEDIA_S3_|ASSETS_IMAGES_S3_|AAP_SOURCE_FENCE_)/i.test(
          key,
        ) &&
        ![
          "MINIO_ROOT_USER",
          "MINIO_ROOT_PASSWORD",
          "AAP_MODULE_DATA_SHARED_BOOTSTRAP_OWNER",
        ].includes(key.toUpperCase()),
    ),
  );
  Object.assign(environment, {
    MINIO_ACCESS_KEY: "",
    MINIO_SECRET_KEY: "",
    MIGRATION_DATABASE_URL: "",
    TEST_DATABASE_URL: "",
    AWS_SHARED_CREDENTIALS_FILE: "/dev/null",
    AWS_CONFIG_FILE: "/dev/null",
    AWS_EC2_METADATA_DISABLED: "true",
    ASSETS_MEDIA_URL: binding.origin,
    ASSETS_MEDIA_TOKEN: binding.token,
    AAP_PRIVATE_FIXTURE_MEDIA_CONTROL_DSN_FILE: binding.control_dsn_file,
    AAP_ASSETS_ADDITIONAL_ORIGINAL_ENDPOINT: original.endpoint,
    AAP_ASSETS_ADDITIONAL_ORIGINAL_BUCKET: original.bucket,
    AAP_ASSETS_ADDITIONAL_ORIGINAL_ACCESS_KEY: original.access_key,
    AAP_ASSETS_ADDITIONAL_ORIGINAL_SECRET_KEY: original.secret_key,
    PYTHONPATH: ".",
  });
  const apiRoot = fileURLToPath(new URL("../../../api/", import.meta.url));
  const child = spawn(
    path.join(apiRoot, ".venv/bin/python"),
    [
      "scripts/seed.py",
      ...arguments_,
      "--profile",
      "screenshots",
      "--run-id",
      runId,
      "--admission-id",
      binding.admission_id,
    ],
    // Keep terminal Ctrl-C from reaching the seed both directly and via our owner.
    { cwd: apiRoot, env: environment, stdio: "inherit", detached: true },
  );
  const failures = [];
  const forwardSignal = () => {
    try {
      // asyncio.run cancels and drains on SIGINT; an unhandled SIGTERM skips that.
      child.kill("SIGINT");
    } catch (error) {
      failures.push(error);
    }
  };
  signal?.addEventListener("abort", forwardSignal, { once: true });
  const result = await new Promise((resolve) => {
    child.on("error", (error) => failures.push(error));
    child.once("close", (code, exitSignal) => resolve({ code, signal: exitSignal }));
  });
  signal?.removeEventListener("abort", forwardSignal);
  if (child.pid !== undefined && result.code !== 0) {
    failures.push(new Error(`Screenshot seed failed: ${result.signal ?? result.code}`));
  }
  if (failures.length === 1) throw failures[0];
  if (failures.length > 1) {
    throw new AggregateError(failures, "Screenshot seed process failed");
  }
}

export function requireScreenshotCaptureClosed(runId) {
  const directory = captureDirectory(runId);
  const supervisor = privateRecord(path.join(directory, "native-supervisor.json"));
  const api = supervisor.api_disposition;
  if (
    supervisor.schema !== "aap-fixture-native-supervisor/v1" ||
    supervisor.suite_id !== runId ||
    supervisor.state !== "retired" ||
    supervisor.api_owned !== true ||
    supervisor.root_origin?.family !== "screenshot" ||
    supervisor.root_origin?.parent_run_id !== null ||
    !Number.isSafeInteger(supervisor.api?.pid) ||
    supervisor.api.pid <= 0 ||
    !Number.isSafeInteger(supervisor.api?.start) ||
    supervisor.api.start <= 0 ||
    !api ||
    api.pid !== supervisor.api?.pid ||
    api.start !== supervisor.api?.start ||
    api.exit_code !== 0 ||
    api.pidfd_exit !== true ||
    api.lifespan_shutdown !== true ||
    api.descendants_drained !== true ||
    typeof supervisor.authority_nonce !== "string" ||
    !/^[a-f0-9]{64}$/.test(supervisor.authority_nonce) ||
    supervisor.resources?.root !== realpathSync(process.env.AAP_WORKTREE_RESOURCE_ROOT ?? "") ||
    supervisor.resources?.mode !== process.env.YOAO_WORKTREE_MODE ||
    !Array.isArray(supervisor.native_run_ids) ||
    supervisor.native_run_ids.length !== 1 ||
    supervisor.native_run_ids[0] !== runId
  ) {
    throw new Error("Screenshot root and API have not positively retired; resources retained");
  }
}

export function screenshotCaptureWorkers(runId) {
  const directory = captureDirectory(runId);
  const authority = privateRecord(path.join(path.dirname(directory), "owner.json"));
  const current = privateRecord(path.join(directory, "native-supervisor.json"));
  if (
    authority.nonce !== current.authority_nonce ||
    authority.pid !== current.pid ||
    authority.start !== current.start ||
    !isDeepStrictEqual(authority.resources, current.resources)
  ) {
    throw new Error("Screenshot worker authority changed");
  }
  const workers = [];
  for (const entry of readdirSync(path.dirname(directory), { withFileTypes: true })) {
    if (!entry.isDirectory() || !/^suite-[a-f0-9]{32}$/.test(entry.name)) continue;
    const hex = entry.name.slice(6);
    const siblingId = `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
    const journal = path.join(captureDirectory(siblingId), "native-supervisor.json");
    if (!lstatSync(journal, { throwIfNoEntry: false })) continue;
    const sibling = privateRecord(journal);
    if (
      sibling.schema === "aap-fixture-native-supervisor/v1" &&
      sibling.suite_id === siblingId &&
      sibling.root_origin?.family === "screenshot" &&
      sibling.root_origin?.parent_run_id === null &&
      canonicalUuid.test(sibling.birth_admission_id) &&
      sibling.state !== "retired" &&
      sibling.authority_nonce === authority.nonce &&
      sibling.pid === authority.pid &&
      sibling.start === authority.start &&
      isDeepStrictEqual(sibling.resources, authority.resources)
    ) {
      workers.push(`aap-fixture-${hex}-media@127.0.0.1`, `aap-fixture-${hex}-pyramid@127.0.0.1`);
    }
  }
  return workers;
}

export async function cleanupScreenshotRun(runId) {
  let failure;
  try {
    const directory = captureDirectory(runId);
    const supervisor = privateRecord(path.join(directory, "native-supervisor.json"));
    const retired = supervisor.state === "retired";
    const generationPath = path.join(directory, "generation.json");
    if (retired) requireScreenshotCaptureClosed(runId);
    if (retired && lstatSync(generationPath, { throwIfNoEntry: false })) {
      const generation = privateRecord(generationPath);
      if (
        !isDeepStrictEqual(generation, {
          resources: supervisor.resources,
          nonce: supervisor.authority_nonce,
          state: "closed",
        })
      ) {
        throw new Error("Screenshot closed authority evidence changed");
      }
    } else {
      const authority = privateRecord(path.join(path.dirname(directory), "owner.json"));
      if (
        authority.nonce !== supervisor.authority_nonce ||
        authority.pid !== supervisor.pid ||
        authority.start !== supervisor.start ||
        !isDeepStrictEqual(authority.resources, supervisor.resources)
      ) {
        throw new Error("Screenshot cleanup authority changed");
      }
      const record = privateRecord(
        path.join(path.dirname(directory), `screenshot-capture-${runId.replaceAll("-", "")}.json`),
      );
      if (
        record.schema !== "yoao-screenshot-capture/v1" ||
        record.run_id !== runId ||
        !canonicalUuid.test(record.operation_id) ||
        !canonicalUuid.test(record.cleanup_operation_id)
      ) {
        throw new Error("Screenshot cleanup operation identity changed");
      }
      // Recovery attaches only to the original live authority, never creates one.
      const prepared = await recoverFixtureOperation(record.operation_id);
      if (
        prepared.status !== 200 ||
        prepared.owner_error ||
        prepared.journal_error ||
        prepared.disposal_error
      ) {
        throw new Error("Screenshot preparation remains unsettled; resources retained");
      }
      await acknowledgeFixtureOperation(record.operation_id);
      if (!retired) {
        const response = await postFixture(
          "http://fixture.local/api/v1/__test/seed/screenshot-cleanup",
          {
            data: { run_id: runId },
            operationId: record.cleanup_operation_id,
            // Reader waits are bounded by the owner; physical retirement must settle.
            timeout: 0,
          },
        );
        if (!response.ok()) {
          throw new Error(
            `Screenshot cleanup failed: ${response.status()} ${await response.text()}`,
          );
        }
      }
      await shutdownFixtureTransport(runId);
    }
  } catch (error) {
    failure = error;
  }
  try {
    await closeFixtureTransport();
  } catch (error) {
    if (failure)
      throw new AggregateError([failure, error], "Screenshot cleanup and transport close failed");
    throw error;
  }
  if (failure) throw failure;
}
