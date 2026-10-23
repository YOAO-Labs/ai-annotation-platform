// Independent consumer of the closed Recovery registration read protocol.
import process from "node:process";

const protocol = "aap-identity-registration-status/v1";
let input = "";
for await (const chunk of process.stdin) input += chunk;

try {
  const { url, control, operation } = JSON.parse(input);
  if (!["read_status", "read_deployment_default"].includes(operation)) {
    throw new Error("operation");
  }
  const response = await fetch(`${url}/v1/identity/recovery/registration-status`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${control}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ schema: protocol, operation }),
    signal: AbortSignal.timeout(10000),
  });
  if (response.status !== 200) throw new Error("status");
  const body = await response.text();
  if (Buffer.byteLength(body) > 65536) throw new Error("size");
  const reply = JSON.parse(body);
  if (
    Object.keys(reply).sort().join(",") !== "data,ok,operation,schema" ||
    reply.schema !== protocol ||
    reply.operation !== operation ||
    reply.ok !== true ||
    !reply.data ||
    Object.keys(reply.data).join(",") !== "allow_open_registration" ||
    typeof reply.data.allow_open_registration !== "boolean"
  ) {
    throw new Error("reply");
  }
  process.stdout.write(JSON.stringify(reply.data.allow_open_registration));
} catch {
  process.stderr.write("Registration read peer rejected request or response\n");
  process.exitCode = 1;
}
