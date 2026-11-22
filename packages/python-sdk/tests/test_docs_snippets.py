"""docs-site/dev/sdk/ 页面中 python 代码块的 smoke test。

只做 compile() 语法检查, 防止文档片段烂掉 (方法名拼错由 contract 性质的
人工核对 + SDK 公开 API 测试兜底, 这里不真实执行网络调用)。
"""

import re
from pathlib import Path

import pytest

DOCS_SDK_DIR = Path(__file__).parent.parent.parent.parent / "docs-site" / "dev" / "sdk"

_PY_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _snippets() -> list[tuple[str, int, str]]:
    out = []
    for md in sorted(DOCS_SDK_DIR.glob("*.md")):
        for i, m in enumerate(_PY_BLOCK.finditer(md.read_text(encoding="utf-8"))):
            out.append((md.name, i, m.group(1)))
    return out


@pytest.mark.parametrize(
    "name,idx,code", _snippets(), ids=[f"{n}#{i}" for n, i, _ in _snippets()]
)
def test_docs_python_snippet_compiles(name: str, idx: int, code: str):
    compile(code, f"{name}#snippet{idx}", "exec")


def test_docs_sdk_pages_exist():
    assert (DOCS_SDK_DIR / "quickstart.md").is_file()
    assert (DOCS_SDK_DIR / "cookbook.md").is_file()
