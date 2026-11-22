import js from "@eslint/js";
import vitest from "@vitest/eslint-plugin";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";
import testContracts from "./scripts/eslint-test-contracts.mjs";

const nativeDialogs = [
  { name: "confirm", message: "用 @/components/ui/decisionDialog 的 confirmDialog 确认操作。" },
  { name: "alert", message: "用 alertDialog 或行内字段错误展示提示。" },
  { name: "prompt", message: "用 inputDialog 收集输入；嵌入浏览器可能静默拦截原生弹窗。" },
];

export default tseslint.config(
  {
    ignores: [
      "dist",
      "node_modules",
      "src/api/generated",
      "coverage",
      "playwright-report",
      "test-results",
    ],
  },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": "off",
      "@typescript-eslint/no-explicit-any": "warn",
      "@typescript-eslint/no-unused-vars": [
        "warn",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["src/**/*.test.{ts,tsx}", "src/**/__tests__/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-explicit-any": "off",
    },
  },
  {
    files: ["{src,scripts}/**/*.test.{ts,tsx,mjs}"],
    plugins: { vitest, "test-contracts": testContracts },
    rules: {
      "vitest/no-identical-title": "error",
      "test-contracts/no-bare-expect": "error",
    },
  },
  {
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/**/*.test.{ts,tsx}", "src/**/__tests__/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-globals": ["error", ...nativeDialogs],
      "no-restricted-properties": [
        "error",
        ...nativeDialogs.flatMap(({ name: property, message }) =>
          ["window", "globalThis"].map((object) => ({ object, property, message })),
        ),
      ],
    },
  },
  {
    files: ["e2e/{tests,fixtures,helpers}/**/*.{ts,tsx,mjs}"],
    plugins: { "test-contracts": testContracts },
    rules: {
      "test-contracts/no-response-status-filter": "error",
      "no-restricted-syntax": [
        "error",
        {
          selector:
            "ImportExpression[source.value=/^\\u002fsrc\\u002f/], ImportExpression[source.quasis.0.value.cooked=/^\\u002fsrc\\u002f/]",
          message:
            "产品 E2E 操作已构建的应用，不从 Vite 的 /src/ 加载实现。源码 Worker 等专项放入 e2e/browser-integration。",
        },
      ],
    },
  },
  {
    files: ["src/**/*.tsx"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "JSXAttribute[name.name='style']",
          message:
            "本文件已迁到同名 CSS module。新增样式请加 CSS class; 真正一次性的动态值用 CSS custom property + 局部 eslint-disable-next-line 放行。",
        },
      ],
    },
  },
);
