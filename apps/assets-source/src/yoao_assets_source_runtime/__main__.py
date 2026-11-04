"""Run the private Assets source HTTP process."""

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
        proxy_headers=False,
        server_header=False,
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
