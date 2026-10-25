import { main } from "./src/provider.mjs";

main().catch(() => {
  process.stderr.write("Workspace owner transfer provider startup failed\n");
  process.exitCode = 1;
});
