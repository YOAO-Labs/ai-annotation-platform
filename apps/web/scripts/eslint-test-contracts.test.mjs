import { ESLint } from "eslint";
import { expect, it } from "vitest";

const eslint = new ESLint();

it.each([
  {
    rule: "test-contracts/no-response-status-filter",
    filePath: "e2e/tests/writes.spec.ts",
    invalid: `function target(reply) { return reply["status"]() === 201; }
      page.waitForResponse(target);`,
    valid: `const first = await page.waitForResponse(reply =>
      reply.url() === url && rules.some(reply => reply.ok()));
      expect(first.status()).toBe(201);`,
  },
  {
    rule: "test-contracts/no-bare-expect",
    filePath: "src/Toolbar.test.tsx",
    invalid: "expect(checkbox);",
    valid: `expect(checkbox, "已选中").toBeChecked();
      const failure = expect(operation).rejects.toThrow(); trigger(); await failure;`,
  },
])("$rule 拒绝错例并保留有效的断言", async ({ rule, filePath, invalid, valid }) => {
  const [rejected] = await eslint.lintText(invalid, { filePath });
  const [accepted] = await eslint.lintText(valid, { filePath });
  expect(rejected.messages.map((message) => message.ruleId)).toEqual([rule]);
  expect(accepted.messages).toEqual([]);
});
