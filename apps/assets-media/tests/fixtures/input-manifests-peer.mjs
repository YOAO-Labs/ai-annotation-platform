const schema = "aap-assets-input-manifest/v1";

try {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 65536) throw new Error("request budget exceeded");
  }
  const config = JSON.parse(input);
  const scope = {
    job_id: config.job_id,
    actor_id: config.actor_id,
    project_id: config.project_id,
  };

  async function call(operation, value) {
    const response = await fetch(`${config.origin}/v1/input-manifests/${operation}`, {
      method: "POST",
      headers: {
        authorization: `Bearer ${config.token}`,
        "content-type": "application/json",
        "accept-encoding": "identity",
      },
      body: JSON.stringify(value),
      redirect: "error",
      signal: AbortSignal.timeout(10000),
    });
    let bytes = 0;
    const chunks = [];
    for await (const chunk of response.body) {
      bytes += chunk.byteLength;
      if (bytes > 2 * 1024 * 1024) throw new Error("reply budget exceeded");
      chunks.push(chunk);
    }
    const wire = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (response.status !== 200 || wire.schema !== schema || wire.ok !== true)
      throw new Error("manifest operation failed");
    return wire.data;
  }

  function checkHeader(header) {
    for (const [name, value] of Object.entries(scope)) {
      if (header[name] !== value) throw new Error("scope changed");
    }
    if (
      header.selection_digest !== config.selection_digest ||
      header.sealed_digest !== config.sealed_digest ||
      header.status !== "sealed" ||
      header.selected_count !== config.selected_count ||
      header.prepared_count !== header.selected_count ||
      header.ready_count + header.failed_count !== header.selected_count ||
      !Object.hasOwn(header.selection, "batch_id") ||
      !Object.hasOwn(header.selection, "task_ids") ||
      !Number.isSafeInteger(header.selected_count)
    )
      throw new Error("barrier changed");
  }

  checkHeader(await call("record", scope));
  let cursor = 0;
  let pages = 0;
  let first = null;
  let lastTask = "";
  do {
    const page = await call("page", {
      ...scope,
      selection_digest: config.selection_digest,
      sealed_digest: config.sealed_digest,
      cursor,
      page_size: 100,
    });
    checkHeader(page.manifest);
    if (page.cursor !== cursor || page.members.length > 100)
      throw new Error("page boundary changed");
    if (cursor < config.selected_count && page.members.length === 0)
      throw new Error("page omitted remaining members");
    for (const member of page.members) {
      if (
        member.ordinal !== ++cursor ||
        member.project_id !== scope.project_id ||
        member.task_id <= lastTask ||
        !["ready", "failed"].includes(member.state)
      )
        throw new Error("member scope changed");
      lastTask = member.task_id;
      first ??= member;
      if (member.input !== null) {
        for (const [name, value] of Object.entries(scope)) {
          if (member.input[name] !== value) throw new Error("input origin changed");
        }
        if (member.input.task_id !== member.task_id) throw new Error("input task changed");
      }
    }
    pages += 1;
    if (page.next_cursor === null) break;
    if (page.next_cursor !== cursor || cursor >= config.selected_count)
      throw new Error("cursor changed");
  } while (true);
  if (cursor !== config.selected_count) throw new Error("membership incomplete");

  if (first) {
    const receipt = await call("prepare", {
      ...scope,
      selection_digest: config.selection_digest,
      ordinal: first.ordinal,
      task_id: first.task_id,
    });
    for (const [name, value] of Object.entries(scope)) {
      if (receipt[name] !== value) throw new Error("prepare scope changed");
    }
    if (
      receipt.selection_digest !== config.selection_digest ||
      receipt.member.task_id !== first.task_id ||
      receipt.member.ordinal !== first.ordinal ||
      receipt.member.state !== first.state ||
      receipt.member.input?.input_id !== first.input?.input_id
    )
      throw new Error("prepare replay changed");
  }
  checkHeader(await call("seal", { ...scope, selection_digest: config.selection_digest }));
  process.stdout.write(
    JSON.stringify({
      schema,
      job_id: scope.job_id,
      selection_digest: config.selection_digest,
      sealed_digest: config.sealed_digest,
      selected_count: cursor,
      pages,
    }),
  );
} catch {
  process.stderr.write("manifest peer contract check failed\n");
  process.exitCode = 1;
}
