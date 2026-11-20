function memberName(node) {
  if (node.type !== "MemberExpression") return undefined;
  return node.computed ? node.property.value : node.property.name;
}

function responsePredicate(source, argument) {
  if (argument?.type !== "Identifier") return argument;
  for (let scope = source.getScope(argument); scope; scope = scope.upper) {
    const binding = scope.set.get(argument.name);
    if (!binding) continue;
    const definition = binding.defs[0];
    if (definition?.type === "FunctionName") return definition.node;
    if (definition?.type === "Variable") return definition.node.init;
    break;
  }
}

export default {
  rules: {
    "no-bare-expect": {
      meta: {
        type: "problem",
        schema: [],
        messages: { bare: "独立的 expect(...) 没有断言结果；调用 matcher。" },
      },
      create: (context) => ({
        "ExpressionStatement > CallExpression[callee.name='expect']"(node) {
          context.report({ node, messageId: "bare" });
        },
      }),
    },
    "no-response-status-filter": {
      meta: {
        type: "problem",
        schema: [],
        messages: {
          filtered:
            "用方法、端点和资源身份等待响应，不在匹配器中读取响应状态。用 waitForApiWrite 等到写入后再断言 HTTP 状态。",
        },
      },
      create(context) {
        const source = context.sourceCode;
        const checked = new Set();
        return {
          CallExpression(node) {
            if (memberName(node.callee) !== "waitForResponse") return;
            const predicate = responsePredicate(source, node.arguments[0]);
            const parameter = predicate?.params?.[0];
            if (parameter?.type !== "Identifier" || checked.has(predicate)) return;
            checked.add(predicate);
            const response = source
              .getDeclaredVariables(predicate)
              .find((v) => v.identifiers.includes(parameter));
            for (const { identifier } of response?.references ?? []) {
              const member = identifier.parent;
              if (member.object !== identifier || member.parent?.callee !== member) continue;
              if (["ok", "status"].includes(memberName(member))) {
                context.report({ node: member.parent, messageId: "filtered" });
              }
            }
          },
        };
      },
    },
  },
};
