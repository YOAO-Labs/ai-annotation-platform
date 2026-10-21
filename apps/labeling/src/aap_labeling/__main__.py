"""Start the private Labeling class-query owner."""

import uvicorn

from .api import create_app
from .config import RuntimeSettings


def main():
    settings = RuntimeSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
