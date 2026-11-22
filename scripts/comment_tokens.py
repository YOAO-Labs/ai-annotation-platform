from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterator
from functools import cache

from tree_sitter import Node, Parser, Query, QueryCursor
from tree_sitter_language_pack import get_language

Span = tuple[int, int]

_SUFFIXES = {
    "py": "python",
    "pyi": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "mts": "typescript",
    "cts": "typescript",
    "tsx": "tsx",
    "css": "css",
    "scss": "scss",
    "sql": "sql",
    "go": "go",
    "sh": "bash",
    "bash": "bash",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "toml",
    "jsonc": "jsonc",
    "html": "html",
    "htm": "html",
    "vue": "vue",
    "svg": "svg",
    "ini": "ini",
    "dockerfile": "dockerfile",
    "dockerignore": "dockerignore",
}
_NAMES = {
    ".dockerignore": "dockerignore",
    ".gitattributes": "ignore",
    ".gitconfig": "gitconfig",
    ".gitignore": "ignore",
    ".gitmodules": "gitconfig",
    ".ignore": "ignore",
    ".npmignore": "ignore",
    ".npmrc": "npmrc",
    ".nvmrc": "nvmrc",
    ".prettierignore": "ignore",
    "go.mod": "gomod",
    "go.work": "gomod",
    "known_hosts": "known_hosts",
    "known_hosts.example": "known_hosts",
    "manifest.in": "manifest",
    "nginx.conf": "nginx",
}
_COPIED_SKILLS = {
    "agent-browser",
    "design-taste-frontend",
    "redesign-existing-projects",
    "shadcn",
}
_EXTERNAL_DIRS = {"vendor", "third_party", "third-party", "generated", "node_modules"}
_ARCHIVE_PREFIXES = ("docs/adr/archive/", "docs/plans/archive/", "ROADMAP/archive/")
_GENERATED_PATHS = {".codex/environments/environment.toml"}
_LOCK_FILES = {
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "go.sum",
    "go.work.sum",
}
_GENERATED_AVATAR = re.compile(r"apps/web/public/avatars/pixel/pixel-[0-9]+\.svg\Z")
_GENERATED_NAME = re.compile(r"(?:\.lock$|(?:^|\.)(?:generated|autogen|min)\.)")
_TS_CONFIG = re.compile(r"(?:tsconfig|jsconfig)(?:\.[^.]+)*\.json\Z")
_SYSTEMD_UNIT = re.compile(
    r"\.(?:service|timer|socket|target|mount|automount|path|slice|scope|swap)(?:\.in)?\Z"
)


def language(path: str) -> str | None:
    parts = path.split("/")
    name = parts[-1].lower()
    suffix = name.rpartition(".")[2]
    if (
        path.startswith(_ARCHIVE_PREFIXES)
        or _EXTERNAL_DIRS.intersection(parts[:-1])
        or (
            len(parts) >= 4
            and parts[:2] == [".agents", "skills"]
            and parts[2] in _COPIED_SKILLS
        )
        or path in _GENERATED_PATHS
        or name in _LOCK_FILES
        or _GENERATED_AVATAR.fullmatch(path)
        or _GENERATED_NAME.search(name)
    ):
        return None
    if _TS_CONFIG.fullmatch(name):
        return "jsonc"
    if re.fullmatch(r"(?:requirements|constraints)(?:[-.][\w.-]+)?\.(?:txt|in)", name):
        return "requirements"
    if suffix in {"md", "mdx", "markdown", "json"}:
        return None
    if name in _NAMES:
        return _NAMES[name]
    if name == ".env" or name.startswith(".env."):
        return "env"
    if _SYSTEMD_UNIT.search(name) or (
        "systemd" in parts[:-1] and name.endswith(".conf")
    ):
        return "systemd"
    if suffix == "dockerignore":
        return "dockerignore"
    if name.startswith("dockerfile"):
        return "dockerfile"
    if name == "caddyfile" or name.startswith("caddyfile."):
        return "caddyfile"
    return _SUFFIXES.get(suffix)


_TREES = {
    "python": "(comment) @comment",
    "javascript": "[(comment) (html_comment) (hash_bang_line)] @comment",
    "typescript": "[(comment) (html_comment) (hash_bang_line)] @comment",
    "tsx": "[(comment) (html_comment) (hash_bang_line)] @comment",
    "scss": "[(comment) (js_comment)] @comment",
    "go": "(comment) @comment",
    "gomod": "(comment) @comment",
    "bash": '(comment) @comment ["<<" "<<-" "<"] @redirect',
    "yaml": "(comment) @comment",
    "toml": "(comment) @comment",
    "jsonc": "(comment) @comment",
    "html": "(comment) @comment [(script_element) (style_element)] @embedded",
    "vue": "(comment) @comment [(script_element) (style_element)] @embedded",
    "svg": "(Comment) @comment",
}
_GRAMMAR_NAMES = {"jsonc": "json", "svg": "xml"}
_NEWLINE = re.compile(rb"\r\n?|\n")
_LINES = re.compile(rb"([^\r\n]*)(?:\r\n?|\n|$)")
_SPACE = b" \t\f\v"
_BOM = b"\xef\xbb\xbf"


@cache
def _grammar(lang: str) -> tuple[Parser, Query]:
    grammar = get_language(_GRAMMAR_NAMES.get(lang, lang))
    return Parser(grammar), Query(grammar, _TREES[lang])


def _captures(lang: str, source: bytes) -> dict[str, list[Node]]:
    parser, query = _grammar(lang)
    return QueryCursor(query).captures(parser.parse(source).root_node)


def _node_span(node: Node, source: bytes) -> Span:
    end = node.end_byte
    if source[end - 1 : end] == b"\r":
        end -= 1
    return node.start_byte, end


def _line_end(source: bytes, pos: int) -> int:
    newline = _NEWLINE.search(source, pos)
    return newline.start() if newline else len(source)


def _after_line(source: bytes, pos: int) -> int:
    newline = _NEWLINE.search(source, pos)
    return newline.end() if newline else len(source)


def _skip_quote(
    source: bytes, pos: int, escapes: bool, *, doubled: bool = False
) -> int:
    quote = source[pos : pos + 1]
    size = len(source)
    start = pos + 1
    while (close := source.find(quote, start)) != -1:
        if escapes:
            backslash = close - 1
            while backslash >= start and source[backslash] == 92:
                backslash -= 1
            if (close - 1 - backslash) % 2:
                start = close + 1
                continue
        if not doubled or source[close + 1 : close + 2] != quote:
            return close + 1
        start = close + 2
    return size


_SQL_IDENT_START = rb"[A-Za-z_\x80-\xff]"
_SQL_IDENT_BODY = rb"[A-Za-z_0-9\x80-\xff]"
_SQL_TOKENS = re.compile(
    rb"(?P<line>--)|(?P<block>/\*)|(?P<escaped>[Ee]')|(?P<quote>['\"])"
    rb"|(?P<dollar>\$(?:" + _SQL_IDENT_START + _SQL_IDENT_BODY + rb"*)?\$)"
    rb"|" + _SQL_IDENT_START + rb"[A-Za-z_0-9$\x80-\xff]*"
)
_SQL_BLOCK_MARKS = re.compile(rb"/\*|\*/")


def _sql_comments(source: bytes) -> Iterator[Span]:
    pos = 0
    while token := _SQL_TOKENS.search(source, pos):
        pos = token.end()
        match token.lastgroup:
            case "line":
                pos = _line_end(source, pos)
                yield token.start(), pos
            case "block":
                depth = 1
                for mark in _SQL_BLOCK_MARKS.finditer(source, pos):
                    depth += 1 if mark.group() == b"/*" else -1
                    if not depth:
                        pos = mark.end()
                        break
                else:
                    pos = len(source)
                yield token.start(), pos
            case "quote" | "escaped":
                pos = _skip_quote(
                    source, pos - 1, token.lastgroup == "escaped", doubled=True
                )
            case "dollar":
                tag = token.group()
                close = source.find(tag, pos)
                pos = len(source) if close == -1 else close + len(tag)


_CSS_ESCAPE = rb"\\(?:[a-fA-F0-9]{1,6}(?:\r\n|[ \t\r\n\f])?|[^\r\n\f])"
_CSS_ESCAPES = re.compile(_CSS_ESCAPE)
_CSS_TOKENS = re.compile(
    rb"(?P<comment>/\*)|(?P<quote>['\"])|(?P<name>[#@]?(?:[-_a-zA-Z0-9\x80-\xff]|"
    + _CSS_ESCAPE
    + rb")+)"
)
_CSS_BAD_STRING_END = re.compile(rb"\\(?:\r\n|[\s\S])|['\"]|[\n\r\f]")
_CSS_URL_END = re.compile(rb"" + _CSS_ESCAPE + rb"|\)")


def _css_escape_value(token: re.Match[bytes]) -> bytes:
    raw = token.group()[1:]
    if raw[:1] in b"0123456789ABCDEFabcdef":
        value = int(raw.rstrip(), 16)
        return bytes((value,)) if 0 < value < 128 else b"\xff"
    return raw


def _css_comments(source: bytes) -> Iterator[Span]:
    pos = 0
    while token := _CSS_TOKENS.search(source, pos):
        pos = token.end()
        if token.lastgroup == "comment":
            end = source.find(b"*/", pos)
            pos = len(source) if end == -1 else end + 2
            yield token.start(), pos
        elif token.lastgroup == "quote":
            quote = token.group()
            for mark in _CSS_BAD_STRING_END.finditer(source, pos):
                if mark.group() in (quote, b"\n", b"\r", b"\f"):
                    pos = mark.end()
                    break
            else:
                pos = len(source)
        elif source[pos : pos + 1] == b"(":
            name = token.group()
            if b"\\" in name:
                name = _CSS_ESCAPES.sub(_css_escape_value, name)
            if name.lower() != b"url":
                continue
            pos += 1
            probe = pos
            while probe < len(source) and source[probe] in _SPACE + b"\r\n":
                probe += 1
            if source[probe : probe + 1] in (b'"', b"'"):
                continue
            for mark in _CSS_URL_END.finditer(source, pos):
                if mark.group() == b")":
                    pos = mark.end()
                    break
            else:
                pos = len(source)


_LINE_MARKERS = {
    "ignore": rb"^#[^\r\n]*",
    "dockerignore": rb"^#[^\r\n]*",
    "ini": rb"^[ \t]*([#;][^\r\n]*)",
    "systemd": rb"^[ \t]*([#;][^\r\n]*)",
    "known_hosts": rb"^[ \t]*(#[^\r\n]*)",
    "nvmrc": rb"#[^\r\n]*",
    "requirements": rb"(?<!\S)#[^\r\n]*",
    "manifest": rb"(?:^|(?<=\r))[^#\r\n]*((?<!\\)#[^\r\n]*)",
}
_LINE_PATTERNS = {
    lang: re.compile(
        rb"(?:^|(?<=\r))" + pattern[1:] if pattern.startswith(b"^") else pattern,
        re.MULTILINE,
    )
    for lang, pattern in _LINE_MARKERS.items()
}
_ENV_KEY = re.compile(rb"(?:export[ \t]+)?[^=:\r\n]+[=:]")
_ENV_BARE = re.compile(rb"(?:export[ \t]+)?(?:'[^'\r\n]+'|[^=:#\s]+)[ \t]+(#[^\r\n]*)")
_CONFIG_TOKENS = re.compile(rb'\\(?:\r\n|[\s\S])|["#;]')
_CADDY_TOKEN = re.compile(
    rb'#[^\r\n]*|["`]|<<(?P<delimiter>[A-Za-z0-9_-]+)(?=\r?\n)|\S+'
)
_NGINX_TOKEN = re.compile(
    rb"""#[^\r\n]*|["']|[;{}]|(?:\\[\s\S]|\$\{[^}\s;]*\}|[^\s;{\\])+"""
)


def _line_comments(lang: str, source: bytes) -> Iterator[Span]:
    offset = 3 if source.startswith(_BOM) else 0
    pattern = _LINE_PATTERNS[lang]
    for token in pattern.finditer(source[offset:]):
        start, end = token.span(1 if pattern.groups else 0)
        yield start + offset, end + offset


def _env_comments(source: bytes) -> Iterator[Span]:
    pos = 3 if source.startswith(_BOM) else 0
    while pos < len(source):
        end = _line_end(source, pos)
        while pos < end and source[pos] in _SPACE:
            pos += 1
        if source[pos : pos + 1] == b"#":
            yield pos, end
        elif key := _ENV_KEY.match(source, pos, end):
            pos = key.end()
            while pos < end and source[pos] in _SPACE:
                pos += 1
            if source[pos : pos + 1] in (b"'", b'"'):
                pos = _skip_quote(source, pos, True)
                end = _line_end(source, pos)
                while pos < end and source[pos] in _SPACE:
                    pos += 1
                if source[pos : pos + 1] == b"#":
                    yield pos, end
            else:
                if source[pos : pos + 1] == b"#" and pos > key.end():
                    yield pos, end
                elif (inline := source.find(b" #", pos, end)) != -1:
                    yield inline + 1, end
        elif bare := _ENV_BARE.fullmatch(source, pos, end):
            yield bare.span(1)
        pos = _after_line(source, end)


def _ini_comments(lang: str, source: bytes) -> Iterator[Span]:
    pos = 3 if source.startswith(_BOM) else 0
    if lang == "gitconfig":
        while token := _CONFIG_TOKENS.search(source, pos):
            pos = token.end()
            if token.group() == b'"':
                pos = _skip_quote(source, token.start(), True)
            elif token.group() in (b"#", b";"):
                pos = _line_end(source, pos)
                yield token.start(), pos
        return
    for line in _LINES.finditer(source, pos):
        start, end = line.span(1)
        raw = line.group(1)
        first = start + len(raw) - len(raw.lstrip(_SPACE))
        if source[first : first + 1] in (b"#", b";"):
            yield first, end
            continue
        equal = source.find(b"=", first, end)
        key_end = equal if equal != -1 else end
        if (marker := _npmrc_marker(source, first, key_end)) is not None:
            yield marker, end
        elif (
            equal != -1
            and (marker := _npmrc_marker(source, equal + 1, end)) is not None
        ):
            yield marker, end


def _npmrc_marker(source: bytes, start: int, end: int) -> int | None:
    field = source[start:end].strip(_SPACE)
    if len(field) > 1 and field[:1] in (b'"', b"'") and field[:1] == field[-1:]:
        return None
    for token in _CONFIG_TOKENS.finditer(source, start, end):
        if token.group() in (b"#", b";"):
            return token.start()
    return None


def _infra_comments(lang: str, source: bytes) -> Iterator[Span]:
    pos = 3 if source.startswith(_BOM) else 0
    pattern = _CADDY_TOKEN if lang == "caddyfile" else _NGINX_TOKEN
    while token := pattern.search(source, pos):
        raw = token.group()
        pos = token.end()
        if raw.startswith(b"#"):
            yield token.span()
        elif raw == b'"' or raw == (b"'" if lang == "nginx" else b"`"):
            pos = _skip_quote(source, token.start(), raw != b"`")
        elif lang == "caddyfile" and (delimiter := token["delimiter"]):
            _, pos = _heredoc_end(
                source, _after_line(source, pos), delimiter, False, indent=True
            )


_HEREDOC_WORD = re.compile(
    rb"""(?:"(?:\\[\s\S]|[^"\\])*"|'[^']*'|\\[\s\S]|[^ \t\r\n;|&<>()'"\\])+"""
)
_HEREDOC_PARTS = re.compile(rb""""(?:\\[\s\S]|[^"\\])*"|'[^']*'|\\[\s\S]|[^'"\\]+""")
_DOCKER_WORDS = re.compile(
    rb"""(?:"(?:\\[\s\S]|[^"\\])*"|'[^']*'|\\[\s\S]|[^\s'"\\])+"""
)
_DOCKER_HEREDOC = re.compile(rb"[0-9]*<<(?P<chomp>-?)(?P<word>[^<]+)\Z")
_DOCKER_INSTRUCTION = re.compile(
    rb"(?:ONBUILD[ \t]+)?(?:RUN|COPY|ADD)[ \t]+(?:--\S+[ \t]+)*", re.IGNORECASE
)
_DOCKER_DIRECTIVE = re.compile(
    rb"#[ \t]*(syntax|escape|check)[ \t]*=[ \t]*(\S+)[ \t]*\Z", re.IGNORECASE
)
_BLANK_BYTES = bytes.maketrans(
    bytes(range(256)),
    bytes(10 if c == 10 else 13 if c == 13 else 32 for c in range(256)),
)


def _unquote_word(raw: bytes) -> tuple[bytes, bool]:
    parts = []
    quoted = False
    for part in _HEREDOC_PARTS.finditer(raw):
        word = part.group()
        if word.startswith(b"'"):
            parts.append(word[1:-1])
            quoted = True
        elif word.startswith(b'"'):
            parts.append(re.sub(rb'\\(?:\r?\n|(["`$\\]))', rb"\1", word[1:-1]))
            quoted = True
        elif word.startswith(b"\\"):
            parts.append(word[1:] if word[1:] not in (b"\n", b"\r\n") else b"")
            quoted = True
        else:
            parts.append(word)
    return b"".join(parts), quoted


def _heredoc_end(
    source: bytes, pos: int, delimiter: bytes, chomp: bool, *, indent: bool = False
) -> Span:
    for line in _LINES.finditer(source, pos):
        content = line.group(1)
        if indent:
            content = content.lstrip(_SPACE)
        elif chomp:
            content = content.lstrip(b"\t")
        if content == delimiter:
            return line.start(), line.end()
    return len(source), len(source)


def _docker_comments(source: bytes) -> Iterator[Span]:
    pos = 3 if source.startswith(_BOM) else 0
    escape = b"\\"
    directive_names = set()
    directives = True
    fragments = []
    while pos < len(source):
        line = _LINES.match(source, pos)
        assert line is not None
        content = line.group(1)
        pos = line.end()
        trimmed = content.lstrip(_SPACE)
        if trimmed.startswith(b"#"):
            yield line.start() + len(content) - len(trimmed), line.end(1)
            directive = _DOCKER_DIRECTIVE.fullmatch(trimmed) if directives else None
            if directive and directive[1].lower() not in directive_names:
                name = directive[1].lower()
                directive_names.add(name)
                if name == b"escape" and directive[2] in (b"\\", b"`"):
                    escape = directive[2]
            else:
                directives = False
            continue
        directives = False
        if not trimmed:
            continue
        right = content.rstrip(_SPACE)
        if right.endswith(escape) and not right.endswith(escape * 2):
            fragments.append(right[:-1])
            continue
        if fragments:
            fragments.append(content)
            content = b"".join(fragments)
            fragments.clear()
        if b"<<" not in content:
            continue
        arguments = _DOCKER_INSTRUCTION.match(content.lstrip(_SPACE))
        if not arguments or arguments.string[arguments.end() :].lstrip(
            _SPACE
        ).startswith(b"["):
            continue
        for token in _DOCKER_WORDS.finditer(arguments.string, arguments.end()):
            if marker := _DOCKER_HEREDOC.fullmatch(token.group()):
                delimiter, _ = _unquote_word(marker["word"])
                _, pos = _heredoc_end(source, pos, delimiter, bool(marker["chomp"]))


def _shell_logical_end(source: bytes, pos: int, spans: list[Span]) -> int:
    while newline := _NEWLINE.search(source, pos):
        pos = newline.start()
        backslash = pos
        while backslash and source[backslash - 1] == 92:
            backslash -= 1
        if (pos - backslash) % 2 == 0 or any(
            start <= backslash < end for start, end in spans
        ):
            return newline.end()
        pos = newline.end()
    return len(source)


def _bash_comments(source: bytes) -> Iterator[Span]:
    captures = _captures("bash", source)
    direct = [_node_span(node, source) for node in captures.get("comment", ())]
    headers = []
    for node in captures.get("redirect", ()):
        pos = node.start_byte
        if node.parent is None or node.parent.type not in {"ERROR", "heredoc_redirect"}:
            continue
        if not source.startswith(b"<<", pos) or source.startswith(b"<<<", pos):
            continue
        if source[pos - 1 : pos] == b"<":
            continue
        chomp = source.startswith(b"<<-", pos)
        start = pos + (3 if chomp else 2)
        while start < len(source) and source[start] in _SPACE:
            start += 1
        if word := _HEREDOC_WORD.match(source, start):
            headers.append((pos, chomp, word, node.parent))
    headers.sort(key=lambda header: header[0])
    masked = None
    restored = []
    i = 0
    next_header = 0
    while i < len(headers):
        if headers[i][0] < next_header:
            i += 1
            continue
        body_start = _shell_logical_end(source, headers[i][2].end(), direct)
        j = i + 1
        while j < len(headers) and headers[j][0] < body_start:
            j += 1
        ranges = []
        body_pos = body_start
        for start, chomp, word, parent in headers[i:j]:
            delimiter, quoted = _unquote_word(word.group())
            close, end = _heredoc_end(source, body_pos, delimiter, chomp)
            ranges.append((start, word.end(), body_pos, close, end, quoted, parent))
            body_pos = end
        i = j
        next_header = body_pos
        _, _, _, close, _, _, parent = ranges[0]
        ending = (
            parent.named_child(parent.named_child_count - 1)
            if parent.named_child_count
            else None
        )
        if (
            len(ranges) == 1
            and ending is not None
            and ending.type == "heredoc_end"
            and _line_end(source, close) == ending.end_byte
        ):
            continue
        if masked is None:
            masked = bytearray(source)
        masked[body_start:body_pos] = source[body_start:body_pos].translate(
            _BLANK_BYTES
        )
        for start, header_end, body, close, end, quoted, _ in ranges:
            masked[start:header_end] = source[start:header_end].translate(_BLANK_BYTES)
            if not quoted and body < close:
                prefix = b"cat " + source[start:header_end] + b"\n"
                isolated = prefix + source[body:end]
                offset = body - len(prefix)
                for node in _captures("bash", isolated).get("comment", ()):
                    if len(prefix) <= node.start_byte < close - offset:
                        first, last = _node_span(node, isolated)
                        restored.append((first + offset, last + offset))
    if masked is None:
        yield from direct
    else:
        corrected = bytes(masked)
        yield from (
            _node_span(node, corrected)
            for node in _captures("bash", corrected).get("comment", ())
        )
        yield from restored


_RAW_MARKUP = {
    b"iframe",
    b"noembed",
    b"noframes",
    b"noscript",
    b"plaintext",
    b"textarea",
    b"title",
    b"xmp",
}
_SCRIPT_MIME = {
    b"application/ecmascript",
    b"application/javascript",
    b"application/x-ecmascript",
    b"application/x-javascript",
    b"module",
    b"text/ecmascript",
    b"text/javascript",
    b"text/javascript1.0",
    b"text/javascript1.1",
    b"text/javascript1.2",
    b"text/javascript1.3",
    b"text/javascript1.4",
    b"text/javascript1.5",
    b"text/jscript",
    b"text/livescript",
    b"text/x-ecmascript",
    b"text/x-javascript",
}
_SCRIPT_LANG = {
    b"js": "javascript",
    b"jsx": "javascript",
    b"javascript": "javascript",
    b"ts": "typescript",
    b"tsx": "tsx",
    b"typescript": "typescript",
}
_STYLE_LANG = {b"css": "css", b"scss": "scss"}


def _node_bytes(node: Node, source: bytes) -> bytes:
    return source[node.start_byte : node.end_byte]


def _tag_name(tag: Node, source: bytes) -> bytes:
    child = tag.named_child(0) if tag.named_child_count else None
    return (
        _node_bytes(child, source).lower()
        if child is not None and child.type == "tag_name"
        else b""
    )


def _tag_attributes(tag: Node, source: bytes) -> dict[bytes, bytes]:
    attributes = {}
    for attribute in tag.named_children:
        if attribute.type != "attribute" or not attribute.named_child_count:
            continue
        key = attribute.named_child(0)
        value = attribute.named_child(1) if attribute.named_child_count > 1 else None
        if value is not None and value.type == "quoted_attribute_value":
            value = value.named_child(0) if value.named_child_count else None
        attributes.setdefault(
            _node_bytes(key, source).lower(),
            _node_bytes(value, source).strip().lower() if value else b"",
        )
    return attributes


def _embedded_language(element: Node, tag: Node, source: bytes) -> str | None:
    attributes = _tag_attributes(tag, source)
    lang = attributes.get(b"lang", b"")
    mime = attributes.get(b"type", b"").split(b";", 1)[0].rstrip()
    if element.type == "style_element":
        return (
            _STYLE_LANG.get(lang or mime.removeprefix(b"text/") or b"css")
            if mime in (b"", b"text/css", b"text/scss")
            else None
        )
    if mime in (
        b"text/typescript",
        b"application/typescript",
        b"text/tsx",
        b"text/jsx",
    ):
        return _SCRIPT_LANG.get(lang or mime.split(b"/", 1)[1])
    return _SCRIPT_LANG.get(lang or b"js") if not mime or mime in _SCRIPT_MIME else None


def _markup_literal(lang: str, node: Node, source: bytes) -> bool:
    while parent := node.parent:
        if parent.type in {
            "attribute",
            "quoted_attribute_value",
            "start_tag",
            "self_closing_tag",
            "end_tag",
            "doctype",
        }:
            return True
        if parent.type in {"element", "template_element"}:
            if (
                lang == "vue"
                and parent.type == "element"
                and parent.parent is not None
                and parent.parent.type == "document"
            ):
                return True
            tag = parent.named_child(0) if parent.named_child_count else None
            if tag is not None:
                name = _tag_name(tag, source)
                if name in _RAW_MARKUP:
                    return True
                if parent.type == "template_element" and _tag_attributes(
                    tag, source
                ).get(b"lang", b"") not in (b"", b"html"):
                    return True
        node = parent
    return False


def _markup_comments(lang: str, source: bytes) -> Iterator[Span]:
    captures = _captures(lang, source)
    for node in captures.get("comment", ()):
        if not _markup_literal(lang, node, source):
            yield _node_span(node, source)
    for element in captures.get("embedded", ()):
        if element.named_child_count < 2:
            continue
        head = element.named_child(0)
        body = element.named_child(1)
        if (
            head is None
            or body is None
            or body.type != "raw_text"
            or _markup_literal(lang, element, source)
        ):
            continue
        if code := _embedded_language(element, head, source):
            for start, end in comments(code, _node_bytes(body, source)):
                yield body.start_byte + start, body.start_byte + end


def _mapping(node: Node | None) -> dict[bytes, Node]:
    if node is None or node.type != "block_mapping":
        return {}
    result = {}
    for pair in node.named_children:
        key = pair.child_by_field_name("key")
        value = pair.child_by_field_name("value")
        if key is not None and value is not None and value.named_child_count:
            result[key.text.strip(b"'\"")] = value.named_child(0)
    return result


def _default_shell(mapping: dict[bytes, Node]) -> Node | None:
    return _mapping(_mapping(mapping.get(b"defaults")).get(b"run")).get(b"shell")


def _workflow_scripts(root: Node) -> Iterator[Span]:
    for document in root.named_children:
        if document.type != "document" or not document.named_child_count:
            continue
        top = _mapping(document.named_children[-1].named_child(0))
        for job_node in _mapping(top.get(b"jobs")).values():
            job = _mapping(job_node)
            steps = job.get(b"steps")
            if steps is None or steps.type != "block_sequence":
                continue
            runner = job.get(b"runs-on")
            unix = runner is not None and runner.text.strip(b"'\"").startswith(
                (b"ubuntu-", b"macos-")
            )
            default = _default_shell(job) or _default_shell(top)
            for item in steps.named_children:
                if item.type != "block_sequence_item" or not item.named_child_count:
                    continue
                step = _mapping(item.named_child(0).named_child(0))
                run = step.get(b"run")
                if run is None or run.type != "block_scalar":
                    continue
                shell = step.get(b"shell", default)
                if shell is None:
                    if not unix:
                        continue
                elif shell.text.strip(b"'\"").split()[:1] not in ([b"sh"], [b"bash"]):
                    continue
                header, newline, body = run.text.partition(b"\n")
                if not newline or not header.startswith(b"|"):
                    continue
                lines = body.splitlines(keepends=True)
                indent = next(
                    (
                        len(line) - len(line.lstrip(b" "))
                        for line in lines
                        if line.strip()
                    ),
                    0,
                )
                if match := re.match(rb"\|[+-]?([1-9])", header):
                    indent = run.parent.parent.start_point.column + int(match[1])
                offsets = []
                physical = run.start_byte + len(header) + 1
                cooked = bytearray()
                for line in lines:
                    skip = min(indent, len(line) - len(line.lstrip(b" ")))
                    offsets.append((len(cooked), physical + skip))
                    cooked.extend(line[skip:])
                    physical += len(line)
                for a, z in _bash_comments(bytes(cooked)):
                    row = bisect_right(offsets, (a, float("inf"))) - 1
                    start, original = offsets[row]
                    yield original + a - start, original + z - start


def policy_spans(
    path: str, lang: str, source: bytes
) -> Iterator[tuple[int, int, str | None]]:
    if path.startswith("apps/web/") and path.endswith((".ts", ".tsx")):
        for node in sorted(
            _captures(lang, source).get("comment", ()), key=lambda n: n.start_byte
        ):
            block = node.parent
            catch_input = (
                block is not None
                and block.type == "statement_block"
                and block.parent.type == "catch_clause"
                and all(child.type == "comment" for child in block.named_children)
            )
            yield *_node_span(node, source), "eslint-no-empty" if catch_input else None
    elif path.startswith(".github/workflows/") and lang == "yaml":
        parser, query = _grammar(lang)
        root = parser.parse(source).root_node
        combined = [
            (*_node_span(node, source), None)
            for node in QueryCursor(query).captures(root).get("comment", ())
        ]
        combined.extend((a, z, "workflow-bash") for a, z in _workflow_scripts(root))
        yield from sorted(combined)
    else:
        yield from ((a, z, None) for a, z in comments(lang, source))


def comments(lang: str, source: bytes) -> Iterator[Span]:
    if lang == "sql":
        yield from _sql_comments(source)
    elif lang == "css":
        yield from _css_comments(source)
    elif lang == "env":
        yield from _env_comments(source)
    elif lang in {"gitconfig", "npmrc"}:
        yield from _ini_comments(lang, source)
    elif lang in {"caddyfile", "nginx"}:
        yield from _infra_comments(lang, source)
    elif lang == "dockerfile":
        yield from _docker_comments(source)
    elif lang in _LINE_PATTERNS:
        yield from _line_comments(lang, source)
    elif lang == "bash":
        yield from sorted(_bash_comments(source))
    elif lang in {"html", "vue"}:
        yield from sorted(_markup_comments(lang, source))
    else:
        yield from sorted(
            _node_span(node, source)
            for node in _captures(lang, source).get("comment", ())
        )
