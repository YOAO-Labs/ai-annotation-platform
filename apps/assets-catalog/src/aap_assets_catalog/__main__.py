"""Run the standalone Assets catalog HTTP service."""

from __future__ import annotations

import uvicorn

from .api import create_app
from .config import load_config


def main() -> None:
    config = load_config()
    uvicorn.run(
        create_app(config=config),
        host=config.host,
        port=config.port,
        access_log=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
