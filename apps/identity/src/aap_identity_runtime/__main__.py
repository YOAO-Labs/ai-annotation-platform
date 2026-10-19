from __future__ import annotations


def main() -> None:
    import uvicorn

    from .api import create_app
    from .config import Settings

    try:
        settings = Settings()
        app = create_app(settings)
    except Exception:
        raise SystemExit("Identity runtime configuration invalid") from None
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
