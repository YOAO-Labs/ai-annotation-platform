import { defineConfig, mergeConfig } from "vite";
import config from "../../../vite.config";

export default defineConfig(async (env) => {
  const web = await (typeof config === "function" ? config(env) : config);
  return mergeConfig(web, {
    test: {
      include: ["src/test/fixtures/apiRequestGuard.cases.ts"],
    },
  });
});
