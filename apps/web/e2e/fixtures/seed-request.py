"""Run fixture preparation separately from the ordinary API's credentials."""

import asyncio
from contextlib import redirect_stdout
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from sqlalchemy.engine import make_url


_MASK_LIFECYCLE_ENABLED = (
    os.environ.get("E2E_SEED_ENABLED") == "true"
    and os.environ.get("ENVIRONMENT") == "development"
)


class SeedSetupRefused(RuntimeError):
    pass


def prepare(payload):
    if os.environ.get("AAP_WORKTREE_MODE") == "dev":
        raise SeedSetupRefused("Development mode cannot prepare E2E fixtures")
    if os.environ.get("ENVIRONMENT", "development") != "development":
        raise SeedSetupRefused("Fixture setup requires development mode")
    owner = make_url(os.environ["PLAYWRIGHT_E2E_DATABASE_URL"])
    runtime = make_url(os.environ["DATABASE_URL"])
    if (
        owner.get_backend_name() != "postgresql"
        or not owner.database
        or not owner.database.endswith(("_e2e", "_test"))
        or (owner.host, owner.port or 5432, owner.database)
        != (runtime.host, runtime.port or 5432, runtime.database)
    ):
        raise SeedSetupRefused("Fixture setup requires the same disposable database")
    if payload.get("operation") == "mask_slice_lifecycle":
        if not _MASK_LIFECYCLE_ENABLED:
            raise SeedSetupRefused(
                "Mask lifecycle requires explicit E2E development mode"
            )
        if os.environ.get("AAP_WORKTREE_MODE") in {"test", "e2e"} and (
            owner.host not in {"localhost", "127.0.0.1", "::1"} or owner.query
        ):
            raise SeedSetupRefused(
                "Mask lifecycle requires the local worktree database"
            )
    else:
        path = urlsplit(payload["path"])
        if (
            payload.get("operation") is not None
            or path.scheme
            or path.netloc
            or not path.path.startswith("/api/v1/__test/seed/")
            or ".." in path.path.split("/")
        ):
            raise SeedSetupRefused("Only fixture endpoints may use setup credentials")
    os.environ["DATABASE_URL"] = owner.render_as_string(hide_password=False)
    os.environ["MIGRATION_DATABASE_URL"] = ""
    os.environ["ENVIRONMENT"] = "development"
    os.environ["E2E_SEED_ENABLED"] = "true"
    return payload


async def main():
    os.environ["DEBUG"] = "false"
    payload = prepare(json.loads(await asyncio.to_thread(sys.stdin.readline)))
    protocol_output = sys.stdout
    with redirect_stdout(sys.stderr):
        sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "apps/api"))
        import httpx
        from fastapi import FastAPI
        from app.api.v1._test_seed import router
        from app.db.base import engine

        app = FastAPI()
        app.include_router(router, prefix="/api/v1/__test")

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://fixture.local"
            ) as client:
                while True:
                    if payload.get("operation") == "mask_slice_lifecycle":
                        spec = importlib.util.spec_from_file_location(
                            "mask_slice_lifecycle",
                            Path(__file__).with_name("mask-slice-lifecycle.py"),
                        )
                        module = importlib.util.module_from_spec(spec)
                        spec.loader.exec_module(module)
                        result = await module.run(**payload["data"])
                        status, body = 200, json.dumps(result)
                    else:
                        response = await client.post(
                            payload["path"],
                            json=payload.get("data"),
                            headers=payload.get("headers"),
                        )
                        status, body = response.status_code, response.text
                    # Keep imports warm, not privileged database sessions or their locks.
                    await engine.dispose()
                    print(
                        json.dumps({"status": status, "body": body}),
                        file=protocol_output,
                        flush=True,
                    )
                    line = await asyncio.to_thread(sys.stdin.readline)
                    if not line:
                        break
                    payload = prepare(json.loads(line))
        finally:
            await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Never expose a DSN, request body, credential or SQL parameter in setup errors.
        print(f"Fixture setup failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None
