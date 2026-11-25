"""packages/python-sdk/README.md 中 python 代码块的 smoke test。

只做 compile() 语法检查，防止 README 片段出现语法错误；站点文档片段校验归属
YOAO-Labs/YOAO-Docs 仓库。
"""

import re
from pathlib import Path

import pytest

SDK_README = Path(__file__).parent.parent / "README.md"

_PY_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _snippets() -> list[tuple[str, int, str]]:
    text = SDK_README.read_text(encoding="utf-8")
    return [
        (SDK_README.name, i, m.group(1)) for i, m in enumerate(_PY_BLOCK.finditer(text))
    ]


@pytest.mark.parametrize(
    "name,idx,code", _snippets(), ids=[f"{n}#{i}" for n, i, _ in _snippets()]
)
def test_docs_python_snippet_compiles(name: str, idx: int, code: str):
    compile(code, f"{name}#snippet{idx}", "exec")


def test_docs_sdk_readme_exists():
    assert SDK_README.is_file()
