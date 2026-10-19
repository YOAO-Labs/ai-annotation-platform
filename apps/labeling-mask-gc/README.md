# Labeling Mask garbage collection

`aap-labeling-mask-gc` is an independently deployable Python worker for the
Labeling-owned Mask deletion fence. Its portable package contains the frozen
JSON contract, a small HTTP client, and the bounded coordinator. The runtime
package calls only the eight allowlisted `aap_labeling_v1` JSONB functions and
the private Mask GC HTTP service.

The process has no application imports, caller-owned SQL sessions, task model,
Celery, Redis, or S3 client. The byte owner receives cleanup credentials; this
process receives only its native database URL and service tokens.

## Configuration

Provide the four secrets either inline or through a sibling `_FILE` setting:

- `MASK_GC_DATABASE_URL`
- `MASK_GC_TOKEN`
- `MASK_GC_CONTENT_URL`
- `MASK_GC_CONTENT_TOKEN`

Secret files are read with an 8 KiB limit and one trailing newline is removed.
The runtime refuses a URL containing credentials outside the database URL and
does not log secret values. `MASK_GC_HOST`, `MASK_GC_PORT`, and
`MASK_GC_POLL_SECONDS` default to `0.0.0.0`, `8771`, and `5`.

Run one bounded pass with:

```sh
python -m aap_labeling_mask_gc_runtime --once
```

The one-shot command prints one JSON object containing numeric cycle counts.
Without `--once`, the process polls for cleanup work and serves authenticated
private `POST /v1/mask-gc/run` and `POST /v1/mask-gc/progress` routes. Liveness and
readiness are available at `/health/live` and `/health/ready`. The HTTP run
request is capped at 1 MiB; each cycle scans at most 20 pages and claims at most
100 delete operations.

Build the standalone image from the repository root with:

```sh
docker build -f apps/labeling-mask-gc/Dockerfile -t aap-labeling-mask-gc .
```

Run the image with a read-only root filesystem and no S3, Celery, or Redis
settings. The image runs as UID/GID 10003 and contains no source bind mount.
