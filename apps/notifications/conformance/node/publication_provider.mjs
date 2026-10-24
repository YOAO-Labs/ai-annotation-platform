import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";

const protocol = "aap-notifications-immediate-publication/v1";
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const authorizationFields = [
  "restricted",
  "project_id",
  "export_required",
  "member",
  "reviewer",
  "owner",
  "super_admin",
];
let phase = "input";

function allowed(value) {
  if (value === null) return true;
  assert.deepEqual(Object.keys(value).sort(), [...authorizationFields].sort());
  assert.ok(value.project_id === null || uuid.test(value.project_id));
  for (const field of authorizationFields.filter((field) => field !== "project_id")) {
    assert.equal(typeof value[field], "boolean");
  }
  if (!value.restricted) return true;
  if (value.project_id === null) return false;
  return (
    value.owner || value.super_admin || (value.export_required ? value.reviewer : value.member)
  );
}

function validate(value) {
  assert.deepEqual(
    Object.keys(value).sort(),
    ["schema", "user_id", "message", "authorization"].sort(),
  );
  assert.equal(value.schema, protocol);
  assert.equal(typeof value.user_id, "string");
  assert.match(value.user_id, uuid);
  assert.ok(value.message && typeof value.message === "object" && !Array.isArray(value.message));
  if (value.authorization === null) {
    assert.equal(value.message.type, "notifications.sync");
  } else {
    assert.deepEqual(
      Object.keys(value.message).sort(),
      ["id", "type", "target_type", "target_id", "payload", "created_at"].sort(),
    );
    assert.match(value.message.id, uuid);
    assert.match(value.message.target_id, uuid);
  }
  return allowed(value.authorization);
}

async function selfCheck() {
  const schema = JSON.parse(
    await readFile(
      new URL("../../src/aap_notifications/publication.schema.json", import.meta.url),
      "utf8",
    ),
  );
  assert.equal(schema.$id, protocol);
  assert.equal(schema["x-http"].path, "/v1/notifications/publish");
  const rule = schema.$defs.ImmediatePublicationRequest.allOf[0];
  assert.equal(rule.if.properties.authorization.type, "null");
  assert.equal(rule.then.properties.message.$ref, "#/$defs/NotificationSyncEvent");
  assert.equal(rule.else.properties.message.$ref, "#/$defs/NotificationEvent");
  const facts = {
    restricted: true,
    project_id: "60000000-0000-4000-8000-000000000001",
    export_required: true,
    member: true,
    reviewer: false,
    owner: false,
    super_admin: false,
  };
  assert.equal(allowed(facts), false);
  assert.equal(allowed({ ...facts, reviewer: true }), true);
  assert.equal(allowed({ ...facts, owner: true }), true);
  assert.equal(allowed({ ...facts, project_id: null, super_admin: true }), false);
  assert.equal(allowed({ ...facts, restricted: false }), true);
  process.stdout.write("Notifications publication Node self-check passed\n");
}

// This byte-only bridge reaches the private network without importing an owner SDK.
const bridge = `import json, sys, urllib.request, urllib.error
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
value=json.load(sys.stdin)
request=urllib.request.Request(value['url'],data=value['body'].encode('utf-8'),headers=value['headers'],method='POST')
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
try:
    response=opener.open(request,timeout=15)
except urllib.error.HTTPError as error:
    response=error
with response:
    data=response.read(4097)
    if len(data)>4096: raise ValueError('reply too large')
    print(json.dumps({'status':response.status,'body':data.decode('utf-8')}))
`;

async function live() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks);
  const input = JSON.parse(raw.toString("utf8"));
  const expected = validate(input);
  assert.equal(String(expected), process.env.AAP_PUBLICATION_PEER_EXPECTED);
  const container = process.env.AAP_PUBLICATION_PEER_CONTAINER;
  const project = process.env.AAP_PUBLICATION_PEER_PROJECT;
  const origin = new URL(process.env.AAP_PUBLICATION_PEER_ORIGIN);
  assert.equal(origin.origin, "http://identity-notification-publisher:8181");
  assert.equal(origin.pathname, "/");
  assert.equal(origin.username + origin.password + origin.search + origin.hash, "");
  const token = process.env.AAP_PUBLICATION_PEER_TOKEN;
  assert.ok(token && /^[\x21-\x7e]+$/.test(token));
  assert.match(container ?? "", /^[0-9a-f]{64}$/);
  phase = "owned container";
  const inspection = spawnSync("docker", ["inspect", container], { encoding: "utf8" });
  assert.equal(inspection.status, 0);
  const [record] = JSON.parse(inspection.stdout);
  assert.equal(record.Id, container);
  assert.equal(record.State.Running, true);
  assert.equal(record.Config.User, "10003:10003");
  assert.equal(record.Config.Labels["org.aap.acceptance"], "module-data-boundary");
  assert.equal(record.Config.Labels["org.aap.acceptance-id"], project);
  assert.equal(record.Config.Labels["com.docker.compose.service"], "identity-api");
  phase = "publication HTTP";
  const result = spawnSync("docker", ["exec", "-i", container, "python", "-c", bridge], {
    input: JSON.stringify({
      url: origin.origin + "/v1/notifications/publish",
      headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
      body: raw.toString("utf8"),
    }),
    encoding: "utf8",
    timeout: 25000,
    maxBuffer: 8192,
  });
  assert.equal(result.status, 0);
  const response = JSON.parse(result.stdout);
  assert.equal(response.status, 200);
  const reply = JSON.parse(response.body);
  assert.deepEqual(Object.keys(reply).sort(), ["schema", "published"].sort());
  assert.equal(reply.schema, protocol);
  assert.equal(typeof reply.published, "boolean");
  assert.equal(reply.published, expected);
  process.stdout.write(
    JSON.stringify({
      protocol,
      published: reply.published,
      request_sha256: createHash("sha256").update(raw).digest("hex"),
      transport: "owned-container HTTP byte bridge, no owner SDK",
    }) + "\n",
  );
}

try {
  if (process.argv.includes("--self-check")) await selfCheck();
  else if (process.argv.includes("--container-http")) await live();
  else throw new Error("choose self-check or owned-container HTTP");
} catch (error) {
  process.stderr.write(
    `Notifications publication Node peer failed at ${phase}: ${error?.name ?? "Error"}\n`,
  );
  process.exitCode = 1;
}
