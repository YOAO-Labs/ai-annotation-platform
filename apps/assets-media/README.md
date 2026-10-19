# Assets media

This package contains the independent video, image and accepted-input runtimes.
The portable `aap-assets-media/v1`, `aap-assets-images/v1` and
`aap-assets-inputs/v1` contracts import only Pydantic and the standard library;
their native installations are owned by migrations 0219, 0220 and 0221.

Video, image and input HTTP routes share one control process and one function-only
control LOGIN. The process verifies all 49 control/system functions before
becoming ready. The video worker LOGIN has 20 functions, including the retained
video input execution and sweep capabilities. Image execution uses a dedicated polling worker with six image and
four input functions, its own SQL LOGIN and its own S3 credentials. The image worker uses
Pillow for small thumbnails and the bounded libvips child process for pyramid
generation. Neither worker imports the API ORM, Celery or Redis.

The image worker also materializes accepted input bytes into the dedicated,
versioned `ASSETS_MEDIA_INPUTS_BUCKET`. Its normal poll processes both lanes;
`--once` processes at most one claim, preferring an input claim. `--job-id` selects
an image job and `--input-id` selects an input reservation. These selectors are
mutually exclusive. Input URLs bind a real S3 VersionId and the recorded digest;
materialization never falls back to a newer source after acceptance. The input
control exposes only `/v1/inputs/find`, `/accept`, `/record`, `/reference` and
`/verify-source`; worker and private source operations are not HTTP routes.

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
[ADR-0106](../../docs/adr/0106-independent-image-media-owner.md). Accepted-input
consumer integration and its required local qualification remain tracked in
[ADR-0107](../../docs/adr/0107-versioned-accepted-media-inputs.md).

Retained video inputs have a separate portable contract and 24 native capabilities.
Their eight new Assets/Labeling state tables, leases, frame plans, lineage pins
and delayed garbage partitions are qualified against owned PostgreSQL resources.
Frame rows are requested lazily. The retained-video libraries are not wired into
the control HTTP routes, worker poll loop or prediction consumers yet; native
installation alone does not establish that complete slice. See
[ADR-0109](../../docs/adr/0109-retained-video-inputs-and-frame-plans.md).
