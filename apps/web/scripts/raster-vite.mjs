import { closeSync, writeSync } from "node:fs";
import { createInterface } from "node:readline";
import { createServer, preview } from "vite";

const mode = process.argv[2];
const listen = {
  host: "127.0.0.1",
  port: Number(process.env.PORT),
  strictPort: true,
};
if (mode !== "preview" && mode !== "dev") {
  throw new Error("Raster Vite owner requires preview or dev mode");
}

const server =
  mode === "preview" ? await preview({ preview: listen }) : await createServer({ server: listen });
if (mode === "dev") await server.listen();
const readyFd = Number(process.env.RASTER_WEB_READY_FD);
writeSync(readyFd, "ready\n");
closeSync(readyFd);

const input = createInterface({ input: process.stdin, terminal: false });
let closed = false;
for await (const command of input) {
  if (command !== "close") throw new Error("Unknown Raster Vite owner command");
  await server.close();
  closed = true;
  break;
}
input.close();
process.stdin.destroy();
if (!closed) throw new Error("Raster Vite owner lost its close control pipe");
