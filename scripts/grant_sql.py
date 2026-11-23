"""Load checked-in grant SQL for psql stdin or the worktree's asyncpg driver."""

from __future__ import annotations

from pathlib import Path
import re


_BASENAME = r"[A-Za-z0-9][A-Za-z0-9_-]*\.sql"
_INCLUDE = re.compile(rf"\\ir[ \t]+({_BASENAME})")
_CONDITION = (
    r"(?:true|false|on|off|:\{\??[A-Za-z_][A-Za-z_0-9]*\}|:[A-Za-z_][A-Za-z_0-9]*)"
)
_CONTROL = re.compile(
    r"\\(?:set[ \t]+(?:ON_ERROR_STOP[ \t]+on|platform_runtime[ \t]+:application_role)"
    rf"|(?:if|elif)[ \t]+{_CONDITION}|else|endif)"
)
_SQL_OR_COMMAND = re.compile(
    r"--[^\n]*|(?P<block>/\*)|"
    r"(?<![\w$])[Ee]'(?:[^'\\]|''|\\[\s\S])*'|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|"
    r"(?<![\w$])(?P<dollar>\$(?:[A-Za-z_]\w*)?\$)[\s\S]*?(?P=dollar)|"
    r"(?P<command>\\[^\n]*)"
)
_BLOCK_DELIMITER = re.compile(r"/\*|\*/")


def _expand(path: Path, *, psql: bool) -> str:
    directory = path.parent.resolve(strict=True)
    seen: set[str] = set()
    visiting: list[str] = []

    def read(name: str) -> str:
        if name in visiting:
            raise ValueError(
                f"Grant SQL include cycle: {' -> '.join([*visiting, name])}"
            )
        if name in seen:
            raise ValueError(f"Duplicate grant SQL include: {name}")
        file = directory / name
        if not re.fullmatch(_BASENAME, name) or file.is_symlink() or not file.is_file():
            raise ValueError(f"Invalid or missing grant SQL file: {file}")
        seen.add(name)
        visiting.append(name)
        sql = file.read_text(encoding="utf-8")
        parts = []
        offset = 0
        last = 0
        while token := _SQL_OR_COMMAND.search(sql, offset):
            offset = token.end()
            if token["block"]:
                depth = 1
                while depth:
                    delimiter = _BLOCK_DELIMITER.search(sql, offset)
                    if delimiter is None:
                        raise ValueError(f"Unclosed grant SQL comment: {file}")
                    depth += 1 if delimiter[0] == "/*" else -1
                    offset = delimiter.end()
            if (command := token["command"]) is None:
                continue
            start = sql.rfind("\n", 0, token.start()) + 1
            if sql[start : token.start()].strip():
                raise ValueError(f"Inline psql command in grant SQL: {file}")
            command = command.rstrip()
            if include := _INCLUDE.fullmatch(command):
                included = read(include[1])
                parts.extend((sql[last:start], included))
                if not included.endswith("\n"):
                    parts.append("\n")
                if offset < len(sql):
                    offset += 1
                last = offset
            elif not (psql and _CONTROL.fullmatch(command)):
                raise ValueError(
                    f"Unsupported grant SQL directive in {file}: {command}"
                )
        parts.append(sql[last:])
        visiting.pop()
        return "".join(parts)

    return read(path.name)


def read_psql_grant_script(path: Path) -> str:
    """Expand literal same-directory includes without requiring files on the psql host."""
    return _expand(path, psql=True)


_NAME = r"[a-z_][a-z0-9_]*"
_QUALIFIED_NAME = rf"{_NAME}\.{_NAME}"
_TYPE = rf"(?:{_QUALIFIED_NAME}|double\s+precision|{_NAME})(?:\[\])*"
_SIGNATURE = rf"{_QUALIFIED_NAME}\(\s*(?:{_TYPE}(?:\s*,\s*{_TYPE})*)?\s*\)"
_RUNTIME_GRANT = re.compile(
    rf"(GRANT\s+(?:USAGE\s+ON\s+SCHEMA\s+{_NAME}(?:\s*,\s*{_NAME})*"
    rf"|EXECUTE\s+ON\s+FUNCTION\s+{_SIGNATURE}(?:\s*,\s*{_SIGNATURE})*"
    rf"|SELECT\s+ON\s+(?:TABLE\s+)?{_QUALIFIED_NAME}(?:\s*,\s*{_QUALIFIED_NAME})*"
    r"|SELECT\s*,\s*INSERT\s+ON\s+(?:TABLE\s+)?yoao_jobs_v1\.async_job_creation)\s+TO)"
    r'\s+:"(?-i:platform_runtime|application_role)"',
    re.IGNORECASE | re.ASCII,
)


def read_runtime_grant_statements(path: Path, *, role: str) -> list[str]:
    """Return single fixed-object GRANTs; only the quoted login name is substituted."""
    if not role or "\0" in role:
        raise ValueError("Invalid runtime grant role")
    quoted_role = '"' + role.replace('"', '""') + '"'
    sql = re.sub(r"--[^\n]*", "", _expand(path, psql=False))
    *parts, tail = sql.split(";")
    if not parts or tail.strip():
        raise ValueError(f"Expected complete runtime GRANT statements: {path}")
    statements = []
    for part in parts:
        match = _RUNTIME_GRANT.fullmatch(part.strip())
        if match is None:
            raise ValueError(
                f"Unsupported runtime grant statement in {path}: {part.strip()}"
            )
        statements.append(f"{match[1]} {quoted_role};")
    return statements
