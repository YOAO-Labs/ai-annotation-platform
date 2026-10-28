# LibreYOLO detection plugin

This service uses the native LibreYOLO 1.6.0 loader, not Ultralytics, and the existing detection SDK, model store and GPU lifecycle. It serves `detect` with YOLO9 t/s/m/c, full images and crops. Controlled checkpoints are independently validated against their immutable training receipts. The separately verified official pretrained files have native metadata version 1.1.1; only exact bytes in the canonical controlled asset manifest are accepted. No weights are downloaded by inference.

## Build and prepare

Build dependency wheels for `apps/_shared/libreyolo_native`, training/task/protocol/backend contracts, and `packages/inference-plugin-sdk`, together with their locked transitive dependencies. Build the plugin wheel from the checkout:

```sh
python plugins/libreyolo/build-wheel.py /absolute/path/to/wheelhouse
```

The build stages `apps/yolo-backend/scripts/controlled-model-assets.json` into the wheel; that file remains the sole maintained source of asset identities. Install from the resulting locked wheelhouse in a clean environment, then explicitly prepare a model:

```sh
aap-libreyolo prepare-assets --size t --asset-dir /private/libreyolo/assets
```

The command downloads only the pinned official source, bounds redirects and size, and checks SHA256 before publication. The default `t` must be prepared before serving; prepare s/m/c separately to expose those variants. Their original model repository licenses must be reviewed before use or redistribution. [LibreYOLO licensing](https://www.libreyolo.com/docs/licensing) identifies the library's code as MIT and YOLO9 COCO checkpoints as MIT, while identifying each exact weight repository as authoritative; code and weight licenses are distinct.

For a local service, supply a private token file containing at least 32 characters and explicitly approved object origins/image hosts:

```sh
export AAP_PLUGIN_ASSET_ORIGINS=https://assets.example
export AAP_PLUGIN_IMAGE_HOSTS=assets.example
export AAP_PLUGIN_DATA_DIR=/private/libreyolo
export AAP_PLUGIN_TOKEN_FILE=/private/libreyolo/token
aap-libreyolo serve --device cpu --asset-dir /private/libreyolo/assets --host 127.0.0.1 --port 8000
```

CPU is a development profile, not GPU qualification. For CUDA set `AAP_PLUGIN_DEVICE=cuda:0`, `AAP_PLUGIN_REGISTRY_ID`, `AAP_PLUGIN_GPU_RESOURCE_ID`, `AAP_PLUGIN_GPU_UUID` (physical `GPU-...`) and `AAP_PLUGIN_GPU_KEYRING_FILE`. The keyring is the existing JSON mapping from key ID to unpadded base64url raw Ed25519 public key; never put authority private keys in the plugin. The CUDA driver is queried for the physical UUID before serving. GPU requests require the existing signed admission and lifecycle contracts as well as the bearer token. CUDA 13.2 build and the 1.6.0/2.14.1+cu132/0.29.1+cu132 package baseline are required; execution is FP32 with half, AMP and TF32 disabled. Failed device checks do not fall back to CPU.

For an OCI artifact provide a digest-pinned base and a complete, separately locked wheelhouse (no Ultralytics):

```sh
python plugins/libreyolo/build-image.py --base-image image@sha256:<digest> --wheelhouse /absolute/path/to/wheelhouse --tag registry.example/libreyolo:release
python plugins/libreyolo/release-manifest.py registry.example/libreyolo:release --device cuda --memory-mb 16384 --gpu-memory-mb 8192 --asset-origin https://assets.example --output manifest.json
```

The image runs as UID/GID 10001, listens on port 8000 and uses `/var/lib/aap-libreyolo` for private state. Its fixed entrypoint is `aap-libreyolo serve --host 0.0.0.0 --port 8000`. Mount prepared assets under `assets/`, with token and public keyring files accessible to that UID. The manifest generator reads the pushed image's actual `RepoDigest`; no placeholder digest qualifies for installation. Installing the artifact does not enable it in a project or prove GPU lifecycle qualification.

## Qualification

`gpu-evidence.py` accepts a controller-prepared scenario with fresh signed lifecycle/admission headers and records actual HTTP responses and `nvidia-smi` observations for at least two prediction/unload cycles. It does not issue authority credentials or mark a deployment qualified. Review recorded results alongside the existing lifecycle evidence, real GPU tensor/device checks, cancellation and release behavior. The training-return trial and project release are separately required; synthetic tests and a successful image build are not qualification.

The lifecycle is not marked evictable by default. Only after the deployment's real GPU lifecycle qualification has passed may the trusted operator set `AAP_PLUGIN_EVICTABLE_VERIFIED=1`; this setting is not itself evidence of qualification.
