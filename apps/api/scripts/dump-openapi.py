#!/usr/bin/env python3
"""把 FastAPI app.openapi() 落到本地 JSON 文件，给 CI / 离线 codegen 用。

用法:
    python apps/api/scripts/dump-openapi.py /tmp/openapi.json

目的：CI 上不需要拉起后端进程，前端 codegen 直接读这个文件即可。
配合 `apps/web/openapi-ts.config.ts` 的 OPENAPI_URL 环境变量：
    OPENAPI_URL=/tmp/openapi.json pnpm codegen
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


_API_ROOT = Path(__file__).resolve().parent.parent
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))


def main() -> int:
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <output_path>", file=sys.stderr)
        return 1

    output = Path(sys.argv[1])
    output.parent.mkdir(parents=True, exist_ok=True)

    from app.main import app

    spec = app.openapi()
    output.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OpenAPI spec written to {output} ({output.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
