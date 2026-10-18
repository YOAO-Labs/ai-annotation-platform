# Assets cleanup worker

This package independently consumes the Assets-owned, committed object-deletion outbox. It has no API application, ORM, Celery, Redis, or broker dependency. PostgreSQL is its only data contract: bucket-filtered keyset pages from `aap_assets_v1.pending_storage_deletions_v2` and acknowledgements through `aap_assets_v1.complete_storage_deletion`. Pages exclude work until the Assets producer's `not_before` deadline, so an in-flight pyramid generation cannot publish objects after cleanup acknowledges its prefix.

The worker validates the exact `image-pyramids/{asset_id}/` prefix, lists and deletes at most 1,000 keys per S3 request, checks partial delete failures, and acknowledges only after the entire prefix is gone. It commits the page-read transaction before object-store IO. A failed request or acknowledgement leaves the outbox row pending; retrying a completed prefix is safe. The keyset cursor advances past failed rows during each scan so one persistent object error cannot block later records.

## Configuration

The process reads only these explicit settings. It never falls back to `DATABASE_URL`, general MinIO settings, ambient AWS credentials, or API configuration.

| Setting                                                             | Required | Default     | Purpose                                              |
| ------------------------------------------------------------------- | -------- | ----------- | ---------------------------------------------------- |
| `ASSET_CLEANUP_DATABASE_URL` or `ASSET_CLEANUP_DATABASE_URL_FILE`   | yes      | —           | PostgreSQL URL for a minimal cleanup LOGIN.          |
| `ASSET_CLEANUP_BUCKET`                                              | yes      | —           | The single bucket accepted from pending outbox rows. |
| `ASSET_CLEANUP_S3_ENDPOINT`                                         | yes      | —           | Explicit S3-compatible HTTP(S) origin.               |
| `ASSET_CLEANUP_S3_ACCESS_KEY` or `ASSET_CLEANUP_S3_ACCESS_KEY_FILE` | yes      | —           | Dedicated list/delete credential.                    |
| `ASSET_CLEANUP_S3_SECRET_KEY` or `ASSET_CLEANUP_S3_SECRET_KEY_FILE` | yes      | —           | Dedicated secret for that credential.                |
| `ASSET_CLEANUP_S3_REGION`                                           | no       | `us-east-1` | S3 signing region.                                   |
| `ASSET_CLEANUP_POLL_SECONDS`                                        | no       | `5`         | Delay between full outbox scans.                     |
| `ASSET_CLEANUP_PAGE_SIZE`                                           | no       | `100`       | Keyset page size, from 1 through 1000.               |
| `ASSET_CLEANUP_HEALTH_HOST`                                         | no       | `127.0.0.1` | Health server bind address; must be IPv4 loopback.   |
| `ASSET_CLEANUP_HEALTH_PORT`                                         | no       | `8765`      | Loopback-only readiness port.                        |

Mount database and object-store credentials as files where the runtime supports secret mounts. The object-store policy needs `ListBucket` constrained to `image-pyramids/*` in the configured bucket and `DeleteObject` under that bucket's `image-pyramids/*` resource. It does not need object reads, writes, bucket administration, or access to another bucket.

Each process scans one configured bucket. When changing the media-cache bucket, keep a cleanup process with its old bucket and credential available until all old-bucket outbox rows have completed; start the new bucket process alongside it for new work.

At startup, the worker verifies that its login is a LOGIN without elevated attributes or role memberships, has no application table/sequence privileges, has no other module capability grants, and can execute exactly the cleanup pending/complete functions. It also probes the configured bucket through the allowed prefix before reporting ready.

`GET /health/live` reports process liveness and `GET /health/ready` reports whether the loop has passed its database and storage checks. Both bind only to loopback inside the container. SIGTERM stops pagination between S3 pages and exits without acknowledging a partially deleted prefix.

## Build and run

Build from the repository root so the independent image copies only this package:

```sh
docker build -f apps/assets-cleanup/Dockerfile -t aap-assets-cleanup:local .
```

Use the repository's module Compose overlay to connect the worker to the local PostgreSQL and MinIO services. The worker has no exposed host port; the loopback readiness endpoint is used by the container health check.

## Contract tests

From this directory:

```sh
uv run --locked --extra worker --group dev pytest
uv run --locked --extra worker --group dev ruff check .
uv run --locked --extra worker --group dev ruff format --check .
```

The frozen protocol schema is in `../api/alembic/sql/0210_asset_storage_cleanup.schema.json`; the package's strict Pydantic models define the corresponding four-field row contract and its timestamped keyset-page extension.
