# Non-Python replacement acceptance fixture

`node_peer.mjs` is a test-only, dependency-free Node.js implementation of the media-probe HTTP contract. It is launched by the acceptance test with an allowlisted environment, binds only to `127.0.0.1` on an ephemeral port, and runs the installed `ffprobe` binary against request bytes written to a private temporary directory. It has no Python runtime, application imports, database access, storage credentials, or broker credentials. This fixture proves that the three operations can be replaced across a language-neutral process boundary; it does not select Node.js for production or add Node to the normal application runtime.

The Python test generates one real CFR MPEG-4 fixture with B frames, then sends the exact same bytes to the Node peer and the standalone Python Media Probe process. It also calls the canonical pure Python probe against the same fixture. Metadata, frame records, packet decode order, presentation ranks, stream counts, and SHA-256 over the exact UTF-8 data lines are compared. A local-only HLS URL reference verifies both implementations reject network protocols without reaching a loopback sentinel. A separate Node self-check covers Python's ties-to-even integer and fractional-fps rounding, including positive and negative midpoint cases.

After `apps/media-probe/src` is present in this checkout, run the acceptance test with the existing API Python environment and installed `node`, `ffmpeg`, and `ffprobe`:

```sh
apps/api/.venv/bin/python -m pytest apps/media-probe/tests/replacement/test_node_peer.py -q
```

For local pre-integration work, pass the separately maintained service source and interpreter explicitly; no path to another worktree is embedded in this test:

```sh
env AAP_MEDIA_PROBE_SOURCE_DIR=/path/to/apps/media-probe/src \
  AAP_MEDIA_PROBE_SERVICE_PYTHON=/path/to/python \
  /path/to/python -m pytest apps/media-probe/tests/replacement/test_node_peer.py -q
```

All child servers, `ffprobe` descendants, uploaded media, and generated fixtures are scoped to the test and cleaned on exit. The tests do not build containers, connect to a database, or call object storage. API adapter switch/failure coverage belongs in the API integration tests.
