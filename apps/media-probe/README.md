# Media probe

`aap-media-probe` owns the ffprobe kernel used to read video metadata, frame timestamps, and packet samples. Its Python kernel only uses the standard library. The standalone HTTP adapter has its own process, bearer token, upload limit, and Docker image; it does not import `apps/api`, open a database, or access platform storage.

## HTTP contract

Every probe request uses `Authorization: Bearer $MEDIA_PROBE_TOKEN` and sends the media bytes directly as `application/octet-stream`. The service never accepts a filesystem path or a URL. Native probing allows only file/pipe protocols, so embedded network references cannot fetch additional input.

| Route                            | Response                                                                                                             |
| -------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `POST /v1/metadata`              | JSON `VideoMetadata`                                                                                                 |
| `POST /v1/frames`                | NDJSON `{ "type": "frame", "data": VideoFrame }` records, then `{ "type": "complete", "count": N, "sha256": "..." }` |
| `POST /v1/packets?start_frame=N` | NDJSON `{ "type": "packet", "data": PacketSample }` records, then the same completion record                         |
| `GET /health`                    | Unauthenticated process health                                                                                       |
| `GET /openapi.json`              | Served API schema, including component schemas for each NDJSON record                                                |

Frame indexes are sequential in frame response order. Packet records keep ffprobe decode order; their `frame_index` values carry the unique presentation-time ranks offset by `start_frame`. The completion digest is lowercase SHA-256 over the exact UTF-8 bytes of all preceding data-record lines, including each newline and excluding the completion line. Clients must read the final completion record and validate both its count and digest before accepting a stream.

Failure responses use `ProbeFailure` JSON with one of `invalid_request`, `file_too_large`, `probe_failed`, or `probe_timeout`. A malformed or empty upload returns 400, an oversized upload 413, a media parse failure 422, a probe timeout 504, and an unexpected probe failure 500. A failed probe is never replaced by a local fallback.

The default `MEDIA_PROBE_MAX_FILE_BYTES` is 1 GiB. Request bodies are copied in bounded chunks to a private temporary file and removed after probing. Disk use can therefore reach the configured limit per concurrent request; the ffprobe JSON result and parsed rows also occupy process memory. The ffprobe subprocess has a fixed 30 second timeout, matching the existing parser behavior. Line size is limited to 64 KiB.

`MEDIA_PROBE_TOKEN` is required at startup and must contain at least 32 non-whitespace ASCII characters. The service reads only that token and `MEDIA_PROBE_MAX_FILE_BYTES`; it has no database, broker, or storage credentials.

## Run locally

From `apps/media-probe`, install the server extra in an isolated environment, install the system `ffprobe` binary, then run:

```sh
uv sync --locked --extra server
export MEDIA_PROBE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
uv run --locked --extra server uvicorn aap_media_probe.app:create_app --factory --host 127.0.0.1 --port 8092
```

The package can also be installed by another local Python project as an editable dependency. Importing `aap_media_probe.probe` does not import FastAPI.

## Build and run the standalone container

Build from the repository root. Supply the token to the container runtime through its environment secret mechanism:

```sh
docker build -f apps/media-probe/Dockerfile -t aap-media-probe:local .
docker run --rm -p 127.0.0.1:8092:8000 \
  -e MEDIA_PROBE_TOKEN="$MEDIA_PROBE_TOKEN" \
  aap-media-probe:local
```

## Contract snapshot and tests

From `apps/media-probe`, check the generated OpenAPI file against the factory output:

```sh
uv run --extra server --extra test python scripts/export_openapi.py --check
uv run --extra server --extra test pytest -q
```

Regenerate the reviewed snapshot after an intentional contract change by omitting `--check`.
