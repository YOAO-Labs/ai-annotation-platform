# Assets media

This package contains the independent video and image media runtimes. The
portable `aap-assets-media/v1` and `aap-assets-images/v1` contracts import only
Pydantic and the standard library; their native installations are owned by
migrations 0219 and 0220.

Video and image HTTP routes share one control process and one function-only
control LOGIN. The process verifies the combined 32 control/system functions
before becoming ready. Video execution remains a separate worker with ten
functions. Image execution uses a dedicated polling worker with six image
functions, its own SQL LOGIN and its own S3 credentials. The image worker uses
Pillow for small thumbnails and the bounded libvips child process for pyramid
generation. Neither worker imports the API ORM, Celery or Redis.

The optional `apps/assets-media/Dockerfile` targets are `control`, `worker` and
`image-worker`. The shared control app is defined in
`docker-compose.assets-media.yml`; its `assets-images-worker` service uses an
explicit image database URL, image S3 identity and legacy annotation bucket.
The default Compose stack remains unchanged. `IMAGE_PYRAMID_AUTO_GENERATE`
defaults to false; enabling it changes admission behavior and should follow the
image capacity runbook.

Video has passed isolated local HTTP/S3/FFmpeg and bounded container acceptance
with separate restricted identities. Image runtime integration does not itself
qualify database migrations, production S3 permissions or sustained image
capacity. The broader ownership and remaining work are tracked in the
[backend module plan](../../docs/plans/1792160873_backend-module-decoupling.md),
[ADR-0105](../../docs/adr/0105-independent-video-media-owner.md) and
[ADR-0106](../../docs/adr/0106-independent-image-media-owner.md).
