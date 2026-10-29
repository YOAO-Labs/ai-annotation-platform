import { existsSync, readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));

export function auditTestRoots(repositoryRoot, registry) {
  const declared = new Set([
    ...registry.ml_cpu.flatMap(({ paths }) =>
      paths.map((path) => `${path.replace(/\/$/, "")}/tests`),
    ),
    ...registry.owner_contracts.map(({ directory }) => `${directory}/tests`),
    ...registry.other_test_roots.flatMap(({ paths }) => paths),
  ]);
  const discovered = [];
  for (const parent of ["apps", "apps/_shared", "packages", "plugins", "docs-site/dev/examples"]) {
    if (!existsSync(join(repositoryRoot, parent))) continue;
    for (const entry of readdirSync(join(repositoryRoot, parent), { withFileTypes: true })) {
      const path = `${parent}/${entry.name}/tests`;
      if (entry.isDirectory() && existsSync(join(repositoryRoot, path))) discovered.push(path);
    }
  }
  if (existsSync(join(repositoryRoot, "apps/api/experiments/tests"))) {
    discovered.push("apps/api/experiments/tests");
  }
  return {
    discovered: discovered.sort(),
    unmapped: discovered.filter((path) => !declared.has(path)).sort(),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const registry = JSON.parse(readFileSync(join(root, "scripts/test-suites.json"), "utf8"));
  const inventory = auditTestRoots(root, registry);
  if (inventory.unmapped.length) {
    console.error(`Test roots without an execution owner: ${inventory.unmapped.join(", ")}`);
    process.exitCode = 1;
  } else if (process.argv.includes("--owner-matrix")) {
    console.log(`matrix=${JSON.stringify({ include: registry.owner_contracts })}`);
  } else {
    console.log(JSON.stringify(inventory, null, 2));
  }
}
