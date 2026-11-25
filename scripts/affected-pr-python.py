import ast
import json
import re
import sys


CALCULATION = re.compile(
    r"(?m)^(?:(?P<day>        for offset in range\(\(month_end - month_start\)\.days)"
    r"(?: [+-] \d+)?\)$|(?P<status>        is_error = int\(row\.status_family\) in )"
    r"\([1-5](?:, [1-5])+,?\)$)"
)


def add_test(before, after):
    if not after.startswith(before):
        return False
    original = ast.parse(before)
    updated = ast.parse(after)
    new_functions = updated.body[len(original.body) :]
    if not new_functions or any(
        ast.dump(old) != ast.dump(new)
        for old, new in zip(original.body, updated.body, strict=False)
    ):
        return False
    names = {f.name for f in original.body if isinstance(f, ast.FunctionDef)}
    if not any(name.startswith("test_") for name in names):
        return False
    for f in new_functions:
        if not (
            isinstance(f, ast.FunctionDef)
            and f.name.startswith("test_")
            and f.name not in names
            and not f.decorator_list
            and not getattr(f, "type_params", [])
            and not (f.args.posonlyargs or f.args.args or f.args.kwonlyargs)
            and not (f.args.vararg or f.args.kwarg)
            and (
                f.returns is None
                or (isinstance(f.returns, ast.Constant) and f.returns.value is None)
            )
            and any(isinstance(s, ast.Assert) for s in f.body)
        ):
            return False
        if not all(
            isinstance(s, ast.Assert)
            or (
                isinstance(s, ast.Assign)
                and all(isinstance(t, ast.Name) for t in s.targets)
            )
            for s in f.body
        ):
            return False
        calls = {
            "build_monthly_summary",
            "SimpleNamespace",
            "date",
            "len",
            "sum",
            "range",
        }
        for node in ast.walk(f):
            if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
                return False
            if isinstance(node, ast.Call) and not (
                isinstance(node.func, ast.Name) and node.func.id in calls
            ):
                return False
            if isinstance(node, (ast.Lambda, ast.NamedExpr, ast.Yield, ast.YieldFrom)):
                return False
        names.add(f.name)
    return True


def approves(kind, before, after):
    if kind == "test":
        return add_test(before, after)
    if kind == "calculation":

        def without_operands(s):
            return CALCULATION.sub(lambda m: m["day"] or m["status"], s)

        return without_operands(before) == without_operands(after)
    return False


if __name__ == "__main__":
    before, after = json.load(sys.stdin)
    try:
        sys.exit(0 if approves(sys.argv[1], before, after) else 1)
    except (SyntaxError, ValueError, RecursionError):
        sys.exit(1)
