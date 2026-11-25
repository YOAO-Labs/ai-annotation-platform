---
name: yoao-doc-media
description: Verify requested UI flows and capture screenshots or recordings on demand. Replace individual documentation or demo media only when requested.
---

# On-demand UI checks and published media

- Load `agent-browser` and its CLI `core` guide. Identify the exact page, role, starting state, requested interaction, and result. Use a fresh named session on an authorized isolated environment with disposable test data and media that can be shared as intended.
- Operate the actual UI. Verify the visible result, relevant requests, and persistence when applicable. Do not inject state, substitute responses, or claim real inference when it did not run.
- Capture a temporary screenshot or recording only if needed; independently check the file, playback, and privacy. Report blockers and what was actually observed. Publish only the requested media and associated references after review, keeping existing paths and accessibility support.
- Do not maintain repository capture scenarios, scripts, image baselines, or scheduled/CI captures. Ordinary functional and rendering tests and their failure diagnostics remain separate.

See [capture and playback checks](references/capture.md) and the [maintainer guide](https://yoao-labs.github.io/YOAO-Docs/dev/how-to/update-screenshots).
