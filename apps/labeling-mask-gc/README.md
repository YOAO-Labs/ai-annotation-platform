# Labeling Mask garbage collection

`aap-labeling-mask-gc` is an independently deployable Python worker for the
Labeling-owned Mask deletion fence. Its portable package contains the frozen
JSON contract, a small HTTP client, and the bounded coordinator. The runtime
package calls only the six cleanup `aap_labeling_v1` JSONB functions and
the private Mask GC HTTP service.

The three write-epoch functions and the running-tracker root registration are
part of the shared contract but denied to the cleanup login. These four producer
gates belong to Labeling's business write transactions.

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
`MASK_GC_POLL_SECONDS` default to `0.0.0.0`, `8771`, and `5`. Set
`MASK_GC_POLL_ENABLED` to strict `true` or `false` (default `true`); `false`
serves HTTP and health routes without starting the background collector, while
authenticated manual run requests remain available.

Run one bounded pass with:

```sh
python -m aap_labeling_mask_gc_runtime --once
```

The one-shot command prints one JSON object containing numeric cycle counts.
Without `--once`, the process polls for cleanup work and serves authenticated
private `POST /v1/mask-gc/run` and `POST /v1/mask-gc/progress` routes. Liveness and
readiness are available at `/health/live` and `/health/ready`. The HTTP run
request is capped at 1 MiB; each cycle scans at most 20 pages and claims at most
100 delete operations. The scan-page and delete-claim budgets are independent.

Send `{"dry_run":true,"scan_pages":2,"after_key":null}` to `/v1/mask-gc/run`
for a nonmutating preview. Each page is scanned through the byte owner and
inspected through the read-only native operation; no durable sweep, staging,
journal, or delete operation is called. `next_after_key` is the physical byte
scan cursor, so pass it as `after_key` on the next dry-run request to continue.
`after_key` is rejected for normal runs. The first successfully inspected page
includes a point-in-time count of currently referenced roots; `referenced` is
null when that observation is unavailable. In dry-run stats, `eligible` is the
preview's eligible count, `skipped` carries the retained count, and `staged`
remains zero. In normal stats, `eligible` reports the actual staged count.

Build the standalone image from the repository root with:

```sh
docker build -f apps/labeling-mask-gc/Dockerfile -t aap-labeling-mask-gc .
```

Run the image with a read-only root filesystem and no S3, Celery, or Redis
settings. The locked server dependencies are installed into the image's virtual
environment, and the process runs as UID/GID 10003 with no source bind mount.
