"""Run the optional private Labeling video-writer HTTP process."""

from __future__ import annotations

import uvicorn

from .video_writer_api import create_video_writer_app
from .video_writer_config import VideoWriterSettings


def main() -> None:
    settings = VideoWriterSettings()
    app = create_video_writer_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
