"""Run the installed Workspace owner without the main API or Celery graph."""

from .api import create_app
from .config import RuntimeSettings


def main():
    import uvicorn

    settings = RuntimeSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
