import assert from "node:assert/strict";
import { test } from "vitest";

import {
  coreBootstrapAborts,
  dashboardViewAborts,
  imageWorkbenchAborts,
  isExpectedRequestAbort,
  maskEditorAborts,
  matchesAbortRule,
  objectCommentsAborts,
  sessionTelemetryAborts,
  taskContextAborts,
  videoMediaAborts,
  videoTaskContextAborts,
  videoWorkbenchAborts,
  type AbortAllowRule,
  type RequestErrorSignal,
} from "../e2e/helpers/request-errors";

const task = "/api/v1/tasks/00000000-0000-0000-0000-000000000000";
const video = "/api/v1/videos/00000000-0000-0000-0000-000000000000";
const project = "/api/v1/projects/00000000-0000-0000-0000-000000000000";
const annotation = "/api/v1/annotations/00000000-0000-0000-0000-000000000000";
const feedback = "/api/v1/feedbacks/00000000-0000-0000-0000-000000000000";
const abort: RequestErrorSignal = { kind: "request", message: "net::ERR_ABORTED" };

function expectAllowed(
  signal: RequestErrorSignal,
  ...ruleGroups: ReadonlyArray<readonly AbortAllowRule[]>
): void {
  assert.equal(
    isExpectedRequestAbort(signal, ...ruleGroups),
    true,
    `${signal.method} ${signal.path} should be an allowed cancellation`,
  );
}

function expectDenied(
  signal: RequestErrorSignal,
  ...ruleGroups: ReadonlyArray<readonly AbortAllowRule[]>
): void {
  assert.equal(
    isExpectedRequestAbort(signal, ...ruleGroups),
    false,
    `${signal.method} ${signal.path} (${signal.kind}, ${signal.message}) must stay an error`,
  );
}

test("every allow rule is structurally auditable, unique and self-matching", () => {
  const groups = {
    sessionTelemetryAborts,
    coreBootstrapAborts,
    dashboardViewAborts,
    taskContextAborts,
    videoTaskContextAborts,
    objectCommentsAborts,
    maskEditorAborts,
    videoMediaAborts,
    imageWorkbenchAborts,
    videoWorkbenchAborts,
  } as const;
  for (const [name, rules] of Object.entries(groups)) {
    assert.ok(rules.length > 0, `${name} must not be empty`);
    const signatures = new Set(rules.map((rule) => `${rule.method} ${String(rule.path)}`));
    assert.equal(signatures.size, rules.length, `${name} must not repeat a method+path rule`);
    for (const rule of rules) {
      assert.ok(rule.reason.trim().length > 0, `${name} rule needs a stated reason`);
      if (typeof rule.path === "string") {
        assert.ok(rule.path.startsWith("/api/"), `${name} string path must stay under /api/`);
        expectAllowed({ ...abort, method: rule.method, path: rule.path }, rules);
      } else {
        assert.equal(rule.path.source.startsWith("^"), true, `${name} regex must be anchored`);
      }
    }
  }
});

test("presets compose their groups without repeating a rule", () => {
  for (const preset of [imageWorkbenchAborts, videoWorkbenchAborts]) {
    const signatures = preset.map((rule) => `${rule.method} ${String(rule.path)}`);
    assert.equal(new Set(signatures).size, signatures.length);
  }
  for (const rule of sessionTelemetryAborts)
    assert.ok(imageWorkbenchAborts.includes(rule) && videoWorkbenchAborts.includes(rule));
  for (const rule of dashboardViewAborts) {
    assert.ok(imageWorkbenchAborts.includes(rule));
    assert.ok(!videoWorkbenchAborts.includes(rule));
  }
  for (const rule of videoTaskContextAborts) {
    assert.ok(videoWorkbenchAborts.includes(rule));
    assert.ok(!imageWorkbenchAborts.includes(rule));
  }
});

test("keepalive session telemetry POSTs are the only write-shaped allowance", () => {
  for (const path of ["/api/v1/auth/me/heartbeat", "/api/v1/auth/me/task-events:batch"]) {
    expectAllowed({ ...abort, method: "POST", path }, imageWorkbenchAborts);
    expectAllowed({ ...abort, method: "POST", path }, videoWorkbenchAborts);
  }
  for (const method of ["GET", "PATCH", "DELETE"] as const)
    expectDenied({ ...abort, method, path: "/api/v1/auth/me/heartbeat" }, sessionTelemetryAborts);
  expectDenied(
    { ...abort, method: "POST", path: "/api/v1/auth/me/preferences" },
    imageWorkbenchAborts,
  );
});

test("image workbench reload allows exactly its observed bootstrap and task reads", () => {
  for (const path of [
    "/api/v1/auth/me",
    "/api/v1/auth/registration-status",
    "/api/v1/feedbacks",
    "/api/v1/projects",
    "/api/v1/tasks",
    "/api/v1/audit-logs",
    task,
    `${task}/annotations`,
    `${task}/discussion/page`,
    `${task}/discussion/annotation-counts`,
    `${project}/access`,
  ])
    expectAllowed({ ...abort, method: "GET", path }, imageWorkbenchAborts);
});

test("video workbench allows exactly the former video lifecycle set", () => {
  for (const path of [
    "/api/v1/auth/me",
    "/api/v1/feedbacks",
    "/api/v1/tasks",
    task,
    `${task}/annotations`,
    `${task}/predictions`,
    `${task}/discussion/page`,
    `${task}/discussion/annotation-counts`,
    `${task}/video/manifest`,
    `${task}/video/manifest-v2`,
    `${task}/video/frames/63`,
    `${task}/video/segments`,
    `${task}/video/frame-timetable`,
    `${video}/chunks/0`,
    `${video}/chunks/0/samples`,
    `${video}/chapters`,
    `${project}/access`,
    `${project}/mention-candidates`,
    `${feedback}/thread`,
  ])
    expectAllowed({ ...abort, method: "GET", path }, videoWorkbenchAborts);
});

test("migration never widens a flow with another flow's allowances", () => {
  // Video-only entries stay out of the image preset.
  for (const path of [
    `${task}/predictions`,
    `${task}/video/manifest`,
    `${task}/video/frames/0`,
    `${video}/chunks/0`,
    `${video}/chapters`,
    `${project}/mention-candidates`,
    `${feedback}/thread`,
  ])
    expectDenied({ ...abort, method: "GET", path }, imageWorkbenchAborts);
  // Image-only dashboard and object reads stay out of the video preset.
  for (const path of [
    "/api/v1/auth/registration-status",
    "/api/v1/projects",
    "/api/v1/audit-logs",
    `${annotation}/comments/page`,
    `${annotation}/mask-content`,
  ])
    expectDenied({ ...abort, method: "GET", path }, videoWorkbenchAborts);
  // Comments panel and mask editor reads only join via explicit composition.
  expectDenied(
    { ...abort, method: "GET", path: `${annotation}/comments/page` },
    imageWorkbenchAborts,
  );
  expectDenied(
    { ...abort, method: "GET", path: `${annotation}/mask-content` },
    imageWorkbenchAborts,
  );
  expectAllowed(
    { ...abort, method: "GET", path: `${annotation}/comments/page` },
    imageWorkbenchAborts,
    objectCommentsAborts,
  );
  expectAllowed(
    { ...abort, method: "GET", path: `${annotation}/mask-content` },
    imageWorkbenchAborts,
    objectCommentsAborts,
    maskEditorAborts,
  );
  // Mask editor content alone never unlocks the comments panel entry.
  expectDenied(
    { ...abort, method: "GET", path: `${annotation}/comments/page` },
    imageWorkbenchAborts,
    maskEditorAborts,
  );
});

test("task queue cancellation is limited to the exact GET lifecycle request", () => {
  expectAllowed({ ...abort, method: "GET", path: "/api/v1/tasks" }, imageWorkbenchAborts);
  expectDenied({ ...abort, method: "POST", path: "/api/v1/tasks" }, imageWorkbenchAborts);
  expectDenied(
    {
      kind: "http",
      message: "Internal Server Error",
      method: "GET",
      path: "/api/v1/tasks",
      status: 500,
    },
    imageWorkbenchAborts,
  );
  expectDenied(
    { ...abort, method: "GET", message: "net::ERR_CONNECTION_RESET", path: "/api/v1/tasks" },
    imageWorkbenchAborts,
  );
  expectDenied({ ...abort, method: "GET", path: "/api/v1/tasks/export" }, imageWorkbenchAborts);
});

test("annotation, Issue and preference writes never become expected cancellations", () => {
  const rules = [...imageWorkbenchAborts, ...objectCommentsAborts, ...maskEditorAborts];
  for (const method of ["POST", "PATCH", "DELETE"] as const)
    for (const path of [
      `${task}/annotations`,
      `${task}/lock`,
      "/api/v1/annotations/a",
      "/api/v1/feedbacks",
      "/api/v1/auth/me/preferences",
    ])
      expectDenied({ ...abort, method, path }, rules);
  expectDenied({ ...abort, method: "DELETE", path: `${task}/lock` }, videoWorkbenchAborts);
});

test("comment count and discussion tab cancellations permit only their exact GET reads", () => {
  for (const path of [`${task}/discussion/annotation-counts`, `${task}/discussion/page`]) {
    const request = { ...abort, method: "GET", path };
    expectAllowed(request, imageWorkbenchAborts);
    expectAllowed(request, videoWorkbenchAborts);
    for (const method of ["POST", "PATCH", "DELETE"] as const)
      expectDenied({ ...request, method }, imageWorkbenchAborts);
    expectDenied(
      { ...request, kind: "http", message: "Not Found", status: 404 },
      imageWorkbenchAborts,
    );
    expectDenied({ ...request, message: "net::ERR_CONNECTION_RESET" }, imageWorkbenchAborts);
    expectDenied({ ...request, path: `${request.path}/export` }, imageWorkbenchAborts);
  }
});

test("Issue thread cancellation permits only its exact aborted GET read", () => {
  expectAllowed({ ...abort, method: "GET", path: `${feedback}/thread` }, videoWorkbenchAborts);
  for (const method of ["POST", "PATCH", "DELETE"] as const)
    expectDenied({ ...abort, method, path: `${feedback}/thread` }, videoWorkbenchAborts);
  for (const path of [`${feedback}/replies`, `${feedback}/thread/export`, feedback])
    expectDenied({ ...abort, method: "GET", path }, videoWorkbenchAborts);
});

test("manifest cancellation permits only exact aborted GET manifest reads", () => {
  for (const path of [`${task}/video/manifest`, `${task}/video/manifest-v2`]) {
    expectAllowed({ ...abort, method: "GET", path }, videoWorkbenchAborts);
    for (const method of ["POST", "PATCH", "DELETE"] as const)
      expectDenied({ ...abort, method, path }, videoWorkbenchAborts);
    expectDenied(
      { ...abort, method: "GET", kind: "http", message: "Bad Gateway", path, status: 502 },
      videoWorkbenchAborts,
    );
    expectDenied({ ...abort, method: "GET", path: `${path}/retry` }, videoWorkbenchAborts);
  }
});

test("HTTP failures, other network failures and unrelated endpoints remain errors", () => {
  const known = { ...abort, method: "GET", path: `${video}/chunks/0/samples` };
  expectAllowed(known, videoWorkbenchAborts);
  expectDenied(
    { ...known, kind: "http", message: "Service Unavailable", status: 503 },
    videoWorkbenchAborts,
  );
  expectDenied({ ...known, message: "net::ERR_CONNECTION_RESET" }, videoWorkbenchAborts);
  expectDenied({ ...known, method: "POST" }, videoWorkbenchAborts);
  for (const path of [`${video}/chunks/0/samples/retry`, `${task}/lock`, "/api/v1/projects/x"])
    expectDenied({ ...known, path }, videoWorkbenchAborts);
});

test("console and page errors never classify as request cancellations", () => {
  for (const kind of ["console", "page"] as const)
    expectDenied(
      { kind, message: "net::ERR_ABORTED", method: "GET", path: "/api/v1/tasks" },
      imageWorkbenchAborts,
    );
});

test("adjacent or deeper lifecycle paths stay errors", () => {
  const rules = [...imageWorkbenchAborts, ...videoWorkbenchAborts];
  for (const path of [
    `${task}/annotations/export`,
    `${task}/annotations/history`,
    `${annotation}/mask-content/preview`,
    `${annotation}/comments`,
    `${project}/access/history`,
    `${task}/video/frames`,
    `${video}/chunks`,
  ])
    expectDenied({ ...abort, method: "GET", path }, rules);
});

test("rule groups compose with fixture rules without weakening denials", () => {
  const fixtureRules: AbortAllowRule[] = [
    {
      method: "DELETE",
      path: /^\/api\/v1\/tasks\/[0-9a-f-]{36}\/lock$/,
      reason: "navigation may cancel the lock release while media is still decoding",
    },
    {
      method: "GET",
      path: "/api/v1/projects",
      reason: "actor switch retires the previous session's project list query",
    },
  ];
  const rules = [...videoWorkbenchAborts, ...fixtureRules];
  expectAllowed({ ...abort, method: "DELETE", path: `${task}/lock` }, rules);
  expectAllowed({ ...abort, method: "GET", path: "/api/v1/projects" }, rules);
  // Without the fixture rules these stay denied, matching the strict video preset.
  expectDenied({ ...abort, method: "DELETE", path: `${task}/lock` }, videoWorkbenchAborts);
  expectDenied({ ...abort, method: "PATCH", path: `${task}/lock` }, rules);
  expectDenied(
    { kind: "http", message: "Conflict", method: "PATCH", path: `${task}/lock`, status: 409 },
    rules,
  );
});

test("matching honours method and pathname exactly for string and regex rules", () => {
  const stringRule: AbortAllowRule = {
    method: "GET",
    path: "/api/v1/tasks",
    reason: "test rule",
  };
  assert.equal(matchesAbortRule({ method: "GET", path: "/api/v1/tasks" }, stringRule), true);
  assert.equal(
    matchesAbortRule({ method: "GET", path: "/api/v1/tasks?pending=true" }, stringRule),
    false,
  );
  assert.equal(matchesAbortRule({ method: "GET" }, stringRule), false);
  const regexRule: AbortAllowRule = {
    method: "DELETE",
    path: /^\/api\/v1\/tasks\/[0-9a-f-]{36}\/lock$/,
    reason: "test rule",
  };
  assert.equal(
    matchesAbortRule(
      { method: "DELETE", path: "/api/v1/tasks/00000000-0000-0000-0000-000000000000/lock" },
      regexRule,
    ),
    true,
  );
  assert.equal(
    matchesAbortRule(
      { method: "DELETE", path: "/api/v1/tasks/00000000-0000-0000-0000-000000000000/lock/release" },
      regexRule,
    ),
    false,
  );
});
