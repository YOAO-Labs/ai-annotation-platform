from __future__ import annotations

import unittest

from comment_tokens import comments, language


class CommentTokensTests(unittest.TestCase):
    def assert_comments(self, lang: str, source: str | bytes, *tokens: str) -> None:
        source = source.encode() if isinstance(source, str) else source
        spans = list(comments(lang, source))
        self.assertEqual(
            [source[start:end] for start, end in spans],
            [token.encode() for token in tokens],
        )
        previous = 0
        for start, end in spans:
            self.assertGreaterEqual(start, previous)
            self.assertGreater(end, start)
            self.assertLessEqual(end, len(source))
            previous = end

    def test_language_ownership(self) -> None:
        paths = {
            ".agents/skills/yoao-runtime/scripts/helper.sh": "bash",
            ".agents/skills/agent-browser/scripts/copied.mjs": None,
            ".agents/skills/design-taste-frontend/demo.tsx": None,
            ".agents/skills/redesign-existing-projects/demo.css": None,
            ".agents/skills/shadcn/demo.tsx": None,
            "apps/api/app/vendor/copied.py": None,
            "a/third_party/fixture.yml": None,
            "third-party/demo.py": None,
            "apps/web/generated/route.ts": None,
            "apps/web/src/ws.generated.ts": None,
            "apps/web/test/fixtures/seed.gen.ts": "typescript",
            "apps/api/openapi.snapshot.json": None,
            "apps/api/tests/migrations/seed.sql": "sql",
            "apps/web/src/vite-env.d.ts": "typescript",
            "apps/web/vite.config.mts": "typescript",
            "apps/web/demo.jsx": "javascript",
            "docs-site/dev/examples/mock-v2-backend/server.py": "python",
            "docs-site/dev/examples/mock-v2-backend/Dockerfile": "dockerfile",
            "docs-site/dev/examples/mock-v2-backend/uv.lock": None,
            "docs/plans/archive/stale.py": None,
            "apps/api/tests/archive/test_import.py": "python",
            ".codex/environments/environment.toml": None,
            "pnpm-lock.yaml": None,
            "apps/web/src/pages/example.tsx": "tsx",
            "apps/web/tsconfig.json": "jsonc",
            "apps/web/tsconfig.node.json": "jsonc",
            "apps/web/jsconfig.json": "jsonc",
            "apps/web/fixture.jsonc": "jsonc",
            "apps/web/package.json": None,
            "apps/web/.env.schema.json": None,
            "docs-site/.vitepress/theme/Home.vue": "vue",
            "apps/web/index.html": "html",
            "apps/web/public/yoao.svg": "svg",
            "apps/web/public/avatars/pixel/pixel-01.svg": None,
            "docs-site/public/diagrams/dev/concepts/system-overview.svg": "svg",
            "docs/ADR.md": None,
            "docs/example.mdx": None,
            "apps/audit-summary/main.go": "go",
            "apps/audit-summary/go.mod": "gomod",
            "infra/docker/Dockerfile.api": "dockerfile",
            "infra/docker/worker.Dockerfile": "dockerfile",
            "apps/training-executor/Dockerfile.dockerignore": "dockerignore",
            "infra/docker/Caddyfile.lan-prod": "caddyfile",
            "infra/docker/nginx.conf": "nginx",
            "infra/systemd/aap-development-api.service.in": "systemd",
            "config/ssh/known_hosts.example": "known_hosts",
            "apps/api/alembic.ini": "ini",
            "apps/api/pyproject.toml": "toml",
            "apps/api/compose.yaml": "yaml",
            "apps/api/compose.yml": "yaml",
            "packages/python-sdk/MANIFEST.in": "manifest",
            "apps/_shared/requirements-plugin-runtime.txt": "requirements",
            "apps/web/.gitignore": "ignore",
            ".env.example": "env",
            ".gitmodules": "gitconfig",
            ".npmrc": "npmrc",
            ".nvmrc": "nvmrc",
        }
        for path, expected in paths.items():
            with self.subTest(path=path):
                self.assertEqual(language(path), expected)

    def test_python_fstrings_and_byte_offsets(self) -> None:
        self.assert_comments(
            "python",
            '#!/usr/bin/env python\r\ntext = """# prose"""\r\n'
            'x = f"""中文 # text {1 # expression\n}"""\r\nprint(r"# text") # 中文\r\n',
            "#!/usr/bin/env python",
            "# expression",
            "# 中文",
        )
        source = 'a = "é"  # 注\n'.encode()
        self.assertEqual(list(comments("python", source)), [(10, 15)])

    def test_javascript_templates_regex_and_jsx(self) -> None:
        self.assert_comments(
            "javascript",
            "#!/usr/bin/node\nconst r = /a[/*]\\//; // regex\n"
            "const t = `// not /* not */ ${2 /* in template */}`;\n"
            'const el = <div data-x="/* no */">// JSX text /* no */ {/* child */}'
            "<b>/* no */</b></div>;\n<!-- historical JS\n",
            "#!/usr/bin/node",
            "// regex",
            "/* in template */",
            "/* child */",
            "<!-- historical JS",
        )
        self.assert_comments(
            "typescript",
            "type X = `// not a comment ${Y /* type */}`; // end\n",
            "/* type */",
            "// end",
        )
        self.assert_comments(
            "tsx",
            "const x: JSX.Element = <div>/* text */{/* actual */}</div>;",
            "/* actual */",
        )

    def test_css_unquoted_and_escaped_url_tokens(self) -> None:
        self.assert_comments(
            "css",
            r"""/* outer */ a { --a: URL(https://host/路径/*path*/); --b: \75 rl(")");
              --c: u\72l(https://a/*also path*/\)b); --d: url("/* literal */" /* token */);
              --e: '/* text */'; /* actual */ } // not a CSS comment""",
            "/* outer */",
            "/* token */",
            "/* actual */",
        )
        self.assert_comments(
            "scss", '$s: "// string"; /* block */\n// line\n', "/* block */", "// line"
        )

    def test_postgresql_nested_comments_and_quoted_bodies(self) -> None:
        self.assert_comments(
            "sql",
            r"""-- start
CREATE FUNCTION f() RETURNS text AS $tag$
-- function body is a SQL string token
SELECT '/* body */';
$tag$ LANGUAGE sql;
SELECT $$-- dollar$$, $标签$/* not */$标签$, 'it''s -- text', E'\'-- text',
       "a""-- column", prefix$tag$; -- tail
/* outer /* nested */ -- interior
still outer */ SELECT 1; -- EOF""",
            "-- start",
            "-- tail",
            "/* outer /* nested */ -- interior\nstill outer */",
            "-- EOF",
        )

    def test_shell_heredocs_redirect_order_and_expansions(self) -> None:
        self.assert_comments(
            "bash",
            "#!/bin/bash\n"
            "cat <<'ONE' <<-TWO # before bodies\n"
            "# literal in one\nONE\n\t# literal in two\n"
            "\t$(echo 'quoted # no' # expansion\n\t)\n\tTWO\n"
            "echo ${value/#pattern/replacement} word#inside \\#escaped # after\n"
            "cat <<'END' # quoted\n$(echo # literal\n)\nEND\n"
            "cat <<HERE\n# literal\n$(echo # shell\n)\nHERE\n# last\n",
            "#!/bin/bash",
            "# before bodies",
            "# expansion",
            "# after",
            "# quoted",
            "# shell",
            "# last",
        )
        self.assert_comments(
            "bash",
            'cat <<A \\\n <<B\n# first\nA\n"\n# second\nB\n# real\n',
            "# real",
        )
        self.assert_comments(
            "bash",
            'echo "<<WORD"; cat <<< "# data"; ((a << b)) # real\n',
            "# real",
        )

    def test_dockerfile_parser_directives_continuations_and_bodies(self) -> None:
        self.assert_comments(
            "dockerfile",
            "\ufeff# syntax=docker/dockerfile:1\r\n# escape=`\r\n"
            "RUN echo '# not a comment' `\r\n  # between continuation lines\r\n    continued\r\n"
            'RUN ["echo", "# JSON data"]\r\nFROM image # not a Docker comment\r\n'
            "ONBUILD COPY <<A `\r\n  <<-'B' /dest/\r\n# first data\r\nA\r\n\t# second data\r\n\tB\r\n"
            "RUN <<EOF\r\n  EOF\r\n# not yet ended\r\nEOF\r\n# done",
            "# syntax=docker/dockerfile:1",
            "# escape=`",
            "# between continuation lines",
            "# done",
        )

    def test_yaml_scalars_toml_strings_and_jsonc(self) -> None:
        self.assert_comments(
            "yaml",
            "# top\nurl: https://host/#anchor # literal fragment above\n"
            'string: "# quoted" # tail\nscript: | # scalar header\n  # recorded in value\nend: true # after\n',
            "# top",
            "# literal fragment above",
            "# tail",
            "# scalar header",
            "# after",
        )
        self.assert_comments(
            "toml",
            '# top\ns = """\n# multiline string text\n""" # tail\nt = \'# literal\'\n',
            "# top",
            "# tail",
        )
        self.assert_comments(
            "jsonc",
            '{ "url": "https://host", /* block */ "x": "/* text */", // tail\n }\n',
            "/* block */",
            "// tail",
        )

    def test_go_directives_and_raw_strings(self) -> None:
        self.assert_comments(
            "go",
            "//go:build linux\npackage main\nconst a = `// text /* text */` // tail\n/* block */",
            "//go:build linux",
            "// tail",
            "/* block */",
        )
        self.assert_comments(
            "gomod",
            "module example.test/foo\nrequire example.test/bar v1.0.0 // indirect\n",
            "// indirect",
        )

    def test_html_embedded_languages_and_raw_text(self) -> None:
        self.assert_comments(
            "html",
            '<!DOCTYPE html><div title="<!-- literal -->">中文</div>\n'
            '<!-- outer --><script TYPE="module">// JavaScript\nconst s = "<!-- text -->";</script>\n'
            '<script type="application/ld+json">{"x":"// text"}</script>'
            "<style>div {color: red; /* CSS */}</style>\n"
            "<textarea><b><!-- text --></textarea><title><!-- text --></title>"
            "<iframe><script>// no embedded script</script></iframe><!-- final -->",
            "<!-- outer -->",
            "// JavaScript",
            "/* CSS */",
            "<!-- final -->",
        )

    def test_vue_template_embedded_typescript_and_scss(self) -> None:
        self.assert_comments(
            "vue",
            '<!-- outer --><template><div title="<!-- literal -->">中文</div><!-- template --></template>\n'
            '<script setup lang="ts">const a: string = "// literal"; // TypeScript\n</script>\n'
            '<style scoped lang="scss">/* block */\n// SCSS\n</style>'
            '<i18n lang="json">{"x":"<!-- custom block data -->"}</i18n>',
            "<!-- outer -->",
            "<!-- template -->",
            "// TypeScript",
            "/* block */",
            "// SCSS",
        )

    def test_svg_preserves_cdata(self) -> None:
        self.assert_comments(
            "svg",
            '<?xml version="1.0"?><svg><![CDATA[<!-- text -->]]><!-- actual --></svg>',
            "<!-- actual -->",
        )

    def test_environment_quotes_and_password_fragments(self) -> None:
        self.assert_comments(
            "env",
            "\ufeff# first\r\nURL=https://domain/#id # tail\r\nMULTI='first\n# literal\nlast' # quoted tail\n"
            'export EMPTY= # explanatory\nPASS=#literal\nD="a\\"# text" # end\nBARE # inherited\n',
            "# first",
            "# tail",
            "# quoted tail",
            "# explanatory",
            "# end",
            "# inherited",
        )

    def test_maintained_configuration_comment_dialects(self) -> None:
        self.assert_comments(
            "ignore",
            "\ufeff# first\r\n\\# filename\npattern#suffix\n # pattern\n# second",
            "# first",
            "# second",
        )
        self.assert_comments(
            "dockerignore", "# first\n # pattern\n# last\n", "# first", "# last"
        )
        self.assert_comments(
            "ini",
            "  ; first\nurl = a#fragment ; value\r\n  # last\n",
            "; first",
            "# last",
        )
        self.assert_comments(
            "systemd",
            "[Service]\n # full\nExecStart=cmd # value\n  ; last\n",
            "# full",
            "; last",
        )
        self.assert_comments(
            "gitconfig",
            '[core] # first\neditor = "x #; y" ; last\n',
            "# first",
            "; last",
        )
        self.assert_comments(
            "npmrc",
            '# first=ignored\nregistry="https://host/#text"\nvalue=a\\#b ; last\n',
            "# first=ignored",
            "; last",
        )
        self.assert_comments(
            "manifest",
            "include lib/* # tail\ninclude a\\#b # literal\n# last\n",
            "# tail",
            "# last",
        )
        self.assert_comments(
            "known_hosts",
            "# first\nhost ssh-ed25519 token # label\n# last\n",
            "# first",
            "# last",
        )
        self.assert_comments("nvmrc", "# first\n22 # LTS\n", "# first", "# LTS")
        self.assert_comments(
            "requirements", "lib @ https://host/a#sha256=123  # pin\n", "# pin"
        )
        self.assert_comments(
            "nginx",
            'server { # first\nproxy_pass https://${host}#path;\nadd_header A "x#y"; # last\n}',
            "# first",
            "# last",
        )
        self.assert_comments(
            "caddyfile",
            'handle /a#path { # first\nrespond "# text"\nrespond <<END\n  # body\n  END\n# last\n}',
            "# first",
            "# last",
        )


if __name__ == "__main__":
    unittest.main()
