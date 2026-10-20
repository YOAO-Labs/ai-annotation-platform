"""
首个 super_admin 引导脚本，与 seed.py 物理隔离，可在 production 安全运行。

用法（推荐通过环境变量传入）：
    cd apps/api
    ADMIN_EMAIL=ops@your-org.com \
    ADMIN_PASSWORD='change-me-now' \
    ADMIN_NAME='平台管理员' \
    uv run python -m scripts.bootstrap_admin
"""

import asyncio
import os
import sys
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.bootstrap_admin import bootstrap_admin
from app.config import settings


def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        print(f"[bootstrap_admin] missing env: {key}", file=sys.stderr)
        raise SystemExit(2)
    return val


async def _bootstrap() -> int:
    email = _require_env("ADMIN_EMAIL").strip().lower()
    password = _require_env("ADMIN_PASSWORD")
    name = _require_env("ADMIN_NAME").strip()

    engine = create_async_engine(
        settings.database_url, echo=False, hide_parameters=True
    )
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        async with Session() as db:
            created = await bootstrap_admin(
                db, email=email, name=name, password=password
            )
            if not created:
                print(f"[bootstrap_admin] user {email} already exists, skip")
                return 0

            await db.commit()
            print(f"[bootstrap_admin] created super_admin {email}")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_bootstrap()))
