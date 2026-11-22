import { defineConfig } from "@hey-api/openapi-ts";
import { resolve } from "path";

export default defineConfig({
  input: process.env.OPENAPI_URL ?? resolve(__dirname, "../api/openapi.snapshot.json"),
  output: {
    path: "src/api/generated",
    format: "prettier",
    lint: "eslint",
  },
  plugins: ["@hey-api/types"],
});
