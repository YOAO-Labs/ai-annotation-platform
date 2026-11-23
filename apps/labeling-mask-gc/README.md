# Labeling Mask garbage collection

`yoao-labeling-mask-gc` is an independently deployable Python worker for the
Labeling-owned Mask deletion fence. Its portable package contains the frozen
JSON contract, a small HTTP client, and the bounded coordinator. The runtime
package calls only the six cleanup `yoao_labeling_v1` JSONB functions and
the private Mask GC HTTP service.

The three write-epoch functions and the running-tracker root registration are
part of the shared contract but denied to the cleanup login. These four producer
gates belong to Labeling's business write transactions.

The process has no application imports, caller-owned SQL sessions, task model,
Celery, Redis, or S3 client. The byte owner receives cleanup credentials; this
process receives only its native database URL and service tokens.

After current migrations and `infra/docker/module-data-grants.sql`, apply
`infra/docker/labeling-mask-gc-data-grants.sql` to
`yoao_labeling_mask_gc_client`. Keep that LOGIN separate from the tracking
worker and the ordinary API; cleanup bytes use a different Assets bearer and
S3 identity. This operator does not replace Beat's saved-object cleanup.

## Operator profile

From the repository root, use a private Compose environment file:

```sh
docker compose --env-file "$HOME/.config/yoao/mask-gc.env" -p yoao-mask-gc \
  -f docker-compose.assets-mask-content.yml -f docker-compose.labeling-mask-gc.yml \
  --profile mask-gc up -d --build --wait labeling-mask-gc
```

Set `LABELING_RUNTIME_MASK_GC_DATABASE_URL_FILE`,
`LABELING_RUNTIME_MASK_GC_CONTROL_TOKEN_FILE`, `MASK_CLEANUP_TOKEN_FILE`,
`MASK_CLEANUP_S3_ACCESS_KEY_FILE`, `MASK_CLEANUP_S3_SECRET_KEY_FILE`,
`MASK_CONTENT_S3_ENDPOINT_URL` and `MASK_CONTENT_BUCKET`. Mount the secrets
read-only from a private host directory, readable by their respective
nonroot container users. The profile starts the cleanup byte peer on the
private network, binds the GC service to localhost and sets
`MASK_GC_POLL_ENABLED=false`. Dry-run over the authenticated HTTP run route is
read-only; `--once` is an explicit mutating collection pass.

With the same Compose project and environment, run the installed one-shot
entrypoint in a fresh container after reviewing the preview:

```sh
docker compose --env-file "$HOME/.config/yoao/mask-gc.env" -p yoao-mask-gc \
  -f docker-compose.assets-mask-content.yml -f docker-compose.labeling-mask-gc.yml \
  --profile mask-gc run --rm --no-deps -T labeling-mask-gc \
  python -m yoao_labeling_mask_gc_runtime --once
```

This requires the existing `assets-mask-cleanup` service to be healthy. It
does not need the GC HTTP service running and uses the same file mounts and
scoped LOGIN as that service.

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
python -m yoao_labeling_mask_gc_runtime --once
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

## Disposable split acceptance

Use `scripts/verify_module_data_boundary.py --output <receipt.json> --test-file
tests/test_mask_gc_runtime_image.py::test_mask_gc_operator_cold` with the
verifier-owned PostgreSQL and MinIO fixtures. Set `AAP_MASK_GC_IMAGE` and
`AAP_MASK_CONTENT_IMAGE` to the immutable image IDs, and
`AAP_MASK_IMAGE_ACCEPTANCE_ID` and `AAP_MASK_IMAGE_SOURCE_REVISION` to their
matching build labels. The test compares the image build inputs against that
revision, boots the shipped Compose services, and recovers an unacknowledged
byte-owner receipt through a fresh `--once` invocation.

Select the entire test file to also enforce the original measured-peak gate
on the same workload. The operational case still requires the 256 MiB kernel
limit; it does not qualify peak usage. If a kernel lacks cgroup v2
`memory.peak`, exact peak measurements are `null` and the strict case fails.
