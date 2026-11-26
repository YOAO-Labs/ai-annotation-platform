# Trainer SDK shared components implementation plan

- **Status.** Completed and verified across all 4 repositories (published `Plugin-SDK` release, remote CI, and RTX 3090 single-GPU OCI smoke passed; all temporary containers and images retired).
- **Release milestone.** Not yet determined.
- **Target repositories.** `YOAO-Labs/Plugin-SDK`, `YOAO-Labs/YOAO-Plugins`, and `YOAO-Labs/YOAO-Docs`.
- **Platform repository impact.** Plan tracking only. `YOAO-Labs/ai-annotation-platform` requires zero runtime changes because `yoao-trainer/v1` and `POLICY_DIGEST` stay byte-identical.

## What changes

The two official trainer plugins (`plugins/trainer/libreyolo` with 2704 source lines and `plugins/trainer/ultralytics` with 5609 source lines) each duplicate about 900 to 1000 lines of platform protocol code. `YOAO-Labs/Plugin-SDK` currently ships only a single 703-line wire-schema file (`packages/python/src/yoao_plugin_sdk/trainer/__init__.py`) and an empty manifest template in `yoao-plugin init --type trainer`.

This plan extracts shared, dependency-free trainer components into `yoao_plugin_sdk.trainer`. It also adds an offline contract verifier (`yoao-plugin trainer check`) and a runnable CPU example template (`yoao-plugin init --type trainer --example`). Both official trainers migrate to the shared modules and delete their duplicated local files in the same wave, followed by CI enforcement and developer documentation.

Fixed non-goals for this plan:

- `yoao-plugin-sdk[trainer]` adds zero new runtime dependencies beyond `pydantic>=2.5`. It never imports `torch`, `torchvision`, `ultralytics`, `libreyolo`, `opencv-python`, `numpy`, or `PyYAML`.
- Framework-private logic stays inside each trainer plugin. That includes `weights_only=True` versus `weights_only=False` checkpoint deserialization, Ultralytics `ema` architecture checks, `InstanceRLEDataset`, `initialization` tensor fingerprints, LibreYOLO `StrictResumeLogging`, `aap_resume` payload validation, framework parameter schemas, cross-field rules, and metric alias stripping.
- `@yoao/plugin-sdk` (`packages/js`) stays scoped to `yoao-trainer/v1` wire validators and TypeScript types. No JS runtime helpers are added while zero Node trainer plugins exist.
- Maintainers assign any `yoao-plugin-sdk` package version bump and artifact publication during the cross-repo handoff gate.

## PR landing order and dependency graph

Land the units in this order: `PR-1`, then `PR-2`, then `PR-3` (review-gated), then `GATE-A` (maintainer artifact handoff), then `PR-4` and `PR-5` in parallel, then `PR-6`, and finally `PR-7` (review-gated).

```text
Plugin-SDK (main)
  PR-1 [branches from Plugin-SDK:main]
    -> PR-2 [waits for PR-1]
         -> PR-3 [waits for PR-2; review-gated CLI interaction]
              -> GATE-A [maintainer builds and publishes Plugin-SDK wheel and manifest]
                   |-> PR-4 [YOAO-Plugins:main; LibreYOLO migration; parallel with PR-5]
                   |-> PR-5 [YOAO-Plugins:main; Ultralytics migration; parallel with PR-4]
                         -> PR-6 [YOAO-Plugins:main; waits for PR-4 and PR-5; CI wiring]
                              -> PR-7 [YOAO-Docs and Plugin-SDK README; waits for PR-3 through PR-6; review-gated]
```

| Unit     | Repository                                                              | Branches from           | Waits for                      | Review-gated | Allowed file paths                                                                                                                                                                                                                                                                                                                   |
| -------- | ----------------------------------------------------------------------- | ----------------------- | ------------------------------ | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `PR-1`   | `YOAO-Labs/Plugin-SDK`                                                  | `main`                  | None                           | No           | `packages/python/src/yoao_plugin_sdk/trainer/{__init__,protocol,files,errors,runtime,membership}.py`, `packages/python/tests/trainer/{__init__,test_runtime_and_files,test_membership}.py`, `packages/python/tests/test_sdk.py`                                                                                                      |
| `PR-2`   | `YOAO-Labs/Plugin-SDK`                                                  | `PR-1`                  | `PR-1`                         | No           | `packages/python/src/yoao_plugin_sdk/trainer/{__init__,dataset,validation,cli}.py`, `packages/python/tests/trainer/{test_dataset_and_validation,test_cli_driver}.py`                                                                                                                                                                 |
| `PR-3`   | `YOAO-Labs/Plugin-SDK`                                                  | `PR-2`                  | `PR-2`                         | Yes          | `packages/python/src/yoao_plugin_sdk/trainer/{check,_scaffold}.py`, `packages/python/src/yoao_plugin_sdk/cli.py`, `packages/python/tests/trainer/test_check_and_scaffold.py`, `packages/python/tests/test_sdk.py`, `.github/workflows/sdk-verify.yml`                                                                                |
| `GATE-A` | Maintainer handoff                                                      | Published `PR-3` commit | `PR-3`                         | Yes          | Maintainer runs `scripts/build-sdk-artifacts.py`, assigns any version bump, publishes manifest, and updates `YOAO-Plugins` variables `YOAO_SDK_MANIFEST` and `YOAO_SDK_MANIFEST_SHA256`                                                                                                                                              |
| `PR-4`   | `YOAO-Labs/YOAO-Plugins`                                                | `main`                  | `GATE-A`                       | No           | `plugins/trainer/libreyolo/**`                                                                                                                                                                                                                                                                                                       |
| `PR-5`   | `YOAO-Labs/YOAO-Plugins`                                                | `main`                  | `GATE-A`                       | No           | `plugins/trainer/ultralytics/**`                                                                                                                                                                                                                                                                                                     |
| `PR-6`   | `YOAO-Labs/YOAO-Plugins`                                                | `main`                  | `PR-4`, `PR-5`                 | No           | `.github/workflows/plugin-trainer.yml`, `README.md`                                                                                                                                                                                                                                                                                  |
| `PR-7`   | `YOAO-Labs/YOAO-Docs`, `YOAO-Labs/Plugin-SDK`, `ai-annotation-platform` | Respective trunks       | `PR-3`, `PR-4`, `PR-5`, `PR-6` | Yes          | `YOAO-Docs/dev/how-to/trainer-plugins.md`, affected links in `YOAO-Docs/dev/concepts/training-datasets.md` and `YOAO-Docs/user-guide/online-learning/{ultralytics,libreyolo}.md`, `Plugin-SDK/{README.md,packages/python/README.md}`, `ai-annotation-platform/docs/plans/1795680269_trainer-sdk-shared-components.md` (`## Outcome`) |

## Architectural sketch and rejected shape

Two package shapes were compared before finalizing `PR-1` through `PR-3`.

- **Rejected shape (flat single module).** Appending file helpers, event emitters, checkpoint rotation, `InputMembership`, dataset validation, CLI parsing, and contract verification into `yoao_plugin_sdk/trainer/__init__.py` grows one file past 2200 lines. Importing `yoao_plugin_sdk.trainer` to validate a `TrainSpecV1` payload would eagerly load `argparse`, `signal`, `subprocess`, and template strings, coupling wire schemas to authoring tools.
- **Chosen shape (layered `yoao_plugin_sdk.trainer` package).** Each submodule owns one concern. `yoao_plugin_sdk/trainer/__init__.py` preserves 100% backward compatibility for existing imports while keeping `check.py` and `_scaffold.py` lazy-imported.

Module boundaries and core signatures:

1. `protocol.py` owns the frozen `yoao-trainer/v1` Pydantic models, `canonical_json_bytes`, `canonical_json_digest`, `atomic_write_json`, and `write_event_jsonl`.
2. `files.py` owns `digest_file(path)`, `sync_directory(path)`, `atomic_write_bytes(path, data, *, mode=0o644)`, `atomic_write_json(path, payload, *, mode=0o644)`, and `immutable_copy(source, destination)` returning `ImmutableArtifactInfo`.
3. `errors.py` owns `TrainerError(ValueError)` with `failure_code`, subclasses `InputMismatchError` (`input_mismatch`), `NativeConfigurationMismatch` (`config_mismatch`), `NativeRuntimeMismatch` (`runtime_mismatch`), `DatasetContractError`, `classify_failure(exc)` (inspecting `sys.modules.get("torch")` without importing `torch`), and `sanitize_failure_message(exc, fallback=...)`.
4. `runtime.py` owns `SUPERVISOR_OWNED_ARTIFACTS`, `EventEmitter(output_dir)` with strict monotonic `seq` methods (`phase`, `epoch`, `progress`, `checkpoint`, `warning`), `build_artifact_entry(role, path)` returning `TrainArtifactV1`, `CheckpointSlotStore(output_dir, *, extension=".pt")` for crash-safe staging, `fsync`, `immutable_copy`, atomic `checkpoint.json` replacement, and previous-slot cleanup, `load_dataset_identity(spec_path)`, and `run_with_signal_receipt(spec_path, output_dir, run_fn, *, logger_prefix=...)` handling `SIGTERM` and `SIGINT` cancellation (`status="cancelled"`, exit code `130`) and exception receipt persistence (`status="failed"`, exit code `1`).
5. `membership.py` owns `MAX_MEMBERS = 20000`, `SPLITS = ("train", "val")`, `MEMBER_FIELDS`, `POLICY_DIGEST` (pinned byte-identical to platform `training_contract.input_membership.POLICY_DIGEST = 1ad70c6b02157e1b4c33646027309a72e73aaa5eba693e7a5fe52ce8f9bb71b4`), `member_records`, `compact_members`, `contract_for`, `observed_members`, `has_member_identities`, and `InputMembership(..., require_identities=True)` whose `complete()` returns `InputMembershipResult(path, proof)` (or `None` when `require_identities=False` and the manifest has no UUID `task_id` entries).
6. `dataset.py` owns pure-stdlib frozen dataset checks (`SOURCE_LINEAGE_CONTRACT`, `ordered_names`, `validate_source_lineage`, `validate_frozen_manifest_core`, `validate_sample_files`, `validate_yolo_bbox_label`). It accepts an already-parsed `data_yaml` mapping or `None`, so `yoao-plugin-sdk[trainer]` never depends on `PyYAML`.
7. `validation.py` owns `normalized(value)`, strict no-coercion `validate_value(value, rule)`, `metrics_of(raw)`, `hash_source_files(paths)`, and `optional_package_version(package, default=None)`.
8. `cli.py` owns the `TrainerPlugin` protocol (`describe()`, `validate_config(payload)`, `train(spec, output_dir, *, spec_path=None)`, and optional `preview(spec, output_dir, *, spec_path=None)`) plus `build_trainer_parser` and `run_trainer_cli(plugin, *, prog, description, supports_preview=False, argv=None)`.
9. `check.py` and `_scaffold.py` stay out of `trainer/__init__.py` and load only when invoked through `yoao-plugin trainer check` or `yoao-plugin init --type trainer --example`.

## Measured trunk baselines

All baselines below were measured directly on the clean repository heads (`Plugin-SDK` `253d7fc`, `YOAO-Plugins` `8dae414`, and `ai-annotation-platform` `c7c75e1af`) on 2026-11-26 with Python 3.12 and Node 20.

| Metric                                           | Probe                                                                                                                                                                                 | Trunk baseline                                                                     |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Platform and plugin `POLICY_DIGEST` parity       | Compare `training_contract.input_membership.POLICY_DIGEST`, `yoao_trainer_libreyolo.membership.POLICY_DIGEST`, and `yoao_trainer_ultralytics.policies.INPUT_MEMBERSHIP_POLICY_DIGEST` | All three equal `1ad70c6b02157e1b4c33646027309a72e73aaa5eba693e7a5fe52ce8f9bb71b4` |
| `yoao_plugin_sdk.trainer` cold import latency    | 5 subprocess runs of `python -c "import yoao_plugin_sdk.trainer"`                                                                                                                     | Median `160.3 ms` (`[310.2, 161.6, 160.3, 158.0, 155.9]`)                          |
| `yoao-plugin version` cold CLI startup           | 5 subprocess runs of `yoao-plugin version`                                                                                                                                            | Median `143.9 ms` (`[304.6, 147.5, 143.9, 138.7, 142.6]`)                          |
| `Plugin-SDK` Python core suite                   | `pytest packages/python/tests/test_sdk.py -q`                                                                                                                                         | `12 passed in 0.23s`                                                               |
| `Plugin-SDK` JS suite                            | `node --test packages/js/test/sdk.test.mjs`                                                                                                                                           | `4 passed in 84.5 ms`                                                              |
| `yoao-trainer-libreyolo` size and CPU suite      | `wc -l` and `pytest -q plugins/trainer/libreyolo/tests`                                                                                                                               | `2704` source lines, `1575` test lines, `65 passed in 2.26s`                       |
| `yoao-trainer-libreyolo describe` cold latency   | 5 subprocess runs of `yoao-trainer-libreyolo describe`                                                                                                                                | Median `189.8 ms` (`[236.7, 182.9, 189.8, 192.0, 183.6]`)                          |
| `yoao-trainer-ultralytics` size and CPU suite    | `wc -l` and `pytest -q plugins/trainer/ultralytics/tests`                                                                                                                             | `5609` source lines, `2253` test lines, `39 passed in 1.36s`                       |
| `yoao-trainer-ultralytics describe` cold latency | 5 subprocess runs of `yoao-trainer-ultralytics describe`                                                                                                                              | Median `190.3 ms` (`[190.3, 190.2, 193.2, 194.4, 189.4]`)                          |

## PR-1 P0 trainer runtime, atomic file I/O, errors, checkpoint store, and input membership (`Plugin-SDK`)

- [ ] **Depends on.** None. Branch directly from `YOAO-Labs/Plugin-SDK:main` in a dedicated worktree.
- [ ] **Review gate.** Standard code review with no CLI interaction change.
- [ ] **Files edited:**
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/protocol.py` (new; moved from `__init__.py`)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/files.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/errors.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/runtime.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/membership.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/__init__.py` (re-export all existing symbols plus P0 API)
  - [ ] `packages/python/tests/trainer/__init__.py` (new)
  - [ ] `packages/python/tests/trainer/test_runtime_and_files.py` (new)
  - [ ] `packages/python/tests/trainer/test_membership.py` (new)
  - [ ] `packages/python/tests/test_sdk.py` (verify backward-compatible `__all__` and zero ML imports)
- [ ] **The one change built:**
  - [ ] Extract `protocol.py` without changing any field, validator, or JSON Schema output of `yoao-trainer/v1` models.
  - [ ] Implement `files.py` (`digest_file`, `sync_directory`, `atomic_write_bytes`, `atomic_write_json`, `immutable_copy`) with temporary file cleanup on `BaseException`, `fsync` before `os.replace`, and directory `fsync`.
  - [ ] Implement `errors.py` (`TrainerError`, `InputMismatchError`, `NativeConfigurationMismatch`, `NativeRuntimeMismatch`, `DatasetContractError`, `classify_failure`, `sanitize_failure_message`) with zero `import torch`.
  - [ ] Implement `runtime.py` (`SUPERVISOR_OWNED_ARTIFACTS`, `EventEmitter`, `build_artifact_entry`, `CheckpointSlotStore`, `load_dataset_identity`, `run_with_signal_receipt`).
  - [ ] Implement `membership.py` (`InputMembership`, `InputMembershipResult`, `POLICY_DIGEST`, `member_records`, `compact_members`, `contract_for`, `observed_members`, `has_member_identities`) preserving byte-identical `POLICY_DIGEST` with platform `training_contract.input_membership`.
- [ ] **Visible result.** Installing `packages/python` with only `pydantic>=2.5` provides all P0 runtime modules and preserves every existing `yoao_plugin_sdk.trainer` import unchanged.
- [ ] **Verification (Unit):**
  - [ ] `pytest packages/python/tests/test_sdk.py packages/python/tests/trainer/test_runtime_and_files.py packages/python/tests/trainer/test_membership.py -q` passes with zero skips.
  - [ ] Behavior assertions cover `immutable_copy` idempotency on identical content and `ValueError` on conflicting content, `CheckpointSlotStore` committing `checkpoint-0001.pt` and `checkpoint.json`, rotating to `checkpoint-0002.pt` and unlinking `0001` only after `checkpoint.json` updates, retaining `0001` when committing `0002` fails, `build_artifact_entry` rejecting every `SUPERVISOR_OWNED_ARTIFACTS` filename, `run_with_signal_receipt` writing `status="cancelled"` on `SIGTERM` (exit 130) and `status="failed"` with `cuda_oom` when given a simulated `torch.cuda.OutOfMemoryError` injected through `sys.modules`, `POLICY_DIGEST == "1ad70c6b02157e1b4c33646027309a72e73aaa5eba693e7a5fe52ce8f9bb71b4"`, and `InputMembership` rejecting path traversal, missing split observation, mid-run split mutation, and duplicate labels.
  - [ ] `node --test packages/js/test/sdk.test.mjs` remains 4 of 4 green.
- [ ] **Verification (Live):**
  - [ ] In the `PR-1` worktree, build a fresh wheel (`python -m build --wheel --no-isolation packages/python`) and install it into a clean venv containing only `pydantic>=2.5`.
  - [ ] Run a real child Python process using `run_with_signal_receipt` that emits `phase`, `epoch`, and `checkpoint` events, receives real `SIGTERM` mid-run, and exits. Pass predicate requires exit code `130`, `result.json` validating against `TrainReceiptV1` with `status == "cancelled"`, `events.jsonl` holding strictly increasing `seq` starting at `1` with valid JSON per line, and zero `.tmp-*` files remaining in output.
  - [ ] Cross-check JSON Schema equality between `yoao_plugin_sdk.trainer` and `ai-annotation-platform/packages/python-sdk/src/training_contract/trainer_plugin.py` plus `POLICY_DIGEST` equality with `training_contract.input_membership`. Pass predicate requires zero schema diff and an identical 64-character hex digest.
- [ ] **Verification (Perf):**
  - [ ] Metric is cold `import yoao_plugin_sdk.trainer` latency (median of 5 subprocess runs on Python 3.12).
  - [ ] Probe runs `python -c "import yoao_plugin_sdk.trainer"` across 5 fresh subprocesses.
  - [ ] Trunk baseline is `160.3 ms` median.
  - [ ] Fail threshold triggers if median exceeds `195 ms` (+20%) or if `sys.modules` contains `torch`, `yaml`, `numpy`, `cv2`, or `httpx` after import.

## PR-2 P1 pure-Python dataset and lineage validators, strict schema value checks, metrics helpers, and `TrainerPlugin` CLI driver (`Plugin-SDK`)

- [ ] **Depends on.** `PR-1`. Branch from `PR-1` in a dedicated worktree.
- [ ] **Review gate.** Standard code review with no new `yoao-plugin` CLI subcommand yet.
- [ ] **Files edited:**
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/dataset.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/validation.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/cli.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/__init__.py` (export P1 helpers)
  - [ ] `packages/python/tests/trainer/test_dataset_and_validation.py` (new)
  - [ ] `packages/python/tests/trainer/test_cli_driver.py` (new)
- [ ] **The one change built:**
  - [ ] Implement `dataset.py` with pure stdlib validation for `SOURCE_LINEAGE_CONTRACT` (all 12 fields, canonical UUID, timezone-aware ISO-8601, positive integer revisions, SHA-256 `path_digest`), `ordered_names`, `validate_frozen_manifest_core` (checking `classes.json`, `manifest.json`, `expected_classes_digest`, optional `expected_snapshot_digest`, and optional caller-parsed `data_yaml` dict), `validate_sample_files` (path containment, symlink rejection, exact image and label SHA-256 verification, required `train` and `val` partitions), and `validate_yolo_bbox_label`.
  - [ ] Implement `validation.py` with `normalized`, strict no-coercion `validate_value(value, rule)` (`anyOf`, `enum`, strict `bool`, `int`, `number`, `string`, and `array`, `minimum`, `maximum`, `exclusiveMinimum`, `multipleOf`, `minItems`, `maxItems`, `uniqueItems`, and `ordered`), `metrics_of` (filtering `bool`, `NaN`, and `Inf` and normalizing `-0.0` to `0.0`), `hash_source_files`, and `optional_package_version`.
  - [ ] Implement `cli.py` with the `TrainerPlugin` protocol, `build_trainer_parser`, and `run_trainer_cli` covering `describe`, `validate-config --spec`, `train --spec --output`, and optional `preview --spec --output`, writing canonical JSON to `stdout` and routing `train` through `run_with_signal_receipt`.
- [ ] **Visible result.** A trainer plugin implements `describe`, `validate_config`, `train`, and optional `preview` on a `TrainerPlugin` object and delegates CLI parsing, canonical stdout printing, dataset lineage checks, and signal receipt handling to `yoao_plugin_sdk.trainer`.
- [ ] **Verification (Unit):**
  - [ ] `pytest packages/python/tests/test_sdk.py packages/python/tests/trainer -q` passes with zero skips.
  - [ ] Behavior assertions cover `validate_source_lineage` accepting valid referenced lineage and rejecting missing contract, extra keys, non-canonical UUID, naive timestamps, and non-positive revisions, `validate_sample_files` rejecting symlinked files, `..` traversal, missing `val` split, and SHA-256 drift, `validate_value` rejecting `True` where `integer` is required (no `bool`-as-`int` coercion), `NaN`, and unordered bounds, `metrics_of` dropping booleans, `inf`, and `nan` and normalizing `-0.0` to `0.0`, and `run_trainer_cli` returning exit `0` with canonical JSON for `describe` and `validate-config`, exit `2` on unreadable spec, exit `0`, `1`, or `130` for `train`, and rejecting `preview` when `supports_preview=False`.
- [ ] **Verification (Live):**
  - [ ] In the `PR-2` worktree, install a dummy console script backed by `run_trainer_cli` and drive it against a read-only (`chmod -R a-w`) synthetic frozen dataset directory across `describe`, `validate-config --spec`, `train --spec --output`, and `preview --spec --output`.
  - [ ] Pass predicate requires `describe` output to validate against `TrainerCapabilitiesV1`, `validate-config` output to validate against `ValidateConfigResultV1`, `train` to write valid `events.jsonl`, `checkpoint.json`, `input-membership.json`, and `result.json` without mutating any file under the read-only dataset directory, and `preview` to write valid `report.json` with `cuda_initialized == False`.
- [ ] **Verification (Perf):**
  - [ ] Metric is cold `import yoao_plugin_sdk.trainer` latency (median of 5 runs) and `validate_source_lineage` plus `InputMembership` throughput on 1000 synthetic members.
  - [ ] Probe runs 5 subprocess imports of `yoao_plugin_sdk.trainer` plus a timed 1000-member lineage and membership verification script.
  - [ ] Trunk baseline is `160.3 ms` cold import median; capture the 1000-member `InputMembership` baseline on `PR-1` before landing `PR-2`.
  - [ ] Fail threshold triggers if cold import median exceeds `200 ms`, if 1000-member lineage and membership check exceeds `250 ms` on CPU, or if `yaml` appears in `sys.modules` after importing `yoao_plugin_sdk.trainer.dataset`.

## PR-3 Offline contract verifier (`yoao-plugin trainer check`) and runnable CPU example template (`Plugin-SDK`, review-gated)

- [ ] **Depends on.** `PR-2`. Branch from `PR-2` in a dedicated worktree.
- [ ] **Review gate.** Review-gated. Introduces the developer-facing `yoao-plugin trainer check` CLI command and `--example` trainer project layout; waits for operator approval before merge.
- [ ] **Files edited:**
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/check.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/trainer/_scaffold.py` (new)
  - [ ] `packages/python/src/yoao_plugin_sdk/cli.py` (add `trainer` command group without importing `[inference]` dependencies and wire `init --type trainer --example`)
  - [ ] `packages/python/tests/trainer/test_check_and_scaffold.py` (new)
  - [ ] `packages/python/tests/test_sdk.py` (test `init --type trainer --example` and `yoao-plugin trainer check`)
  - [ ] `.github/workflows/sdk-verify.yml` (add `packages/python/tests/trainer` and live CLI smoke check to `python-core` matrix)
- [ ] **The one change built:**
  - [ ] Implement `check.py` with `check_trainer_plugin(entrypoint, *, manifest_path=None, skip_train=False, check_preview=False, timeout_seconds=60)` returning `TrainerCheckReport`.
  - [ ] Run checks against the real plugin entrypoint in a temporary directory with a read-only (`0555` and `0444`) synthetic frozen detection dataset:
    1. Optional `manifest.json` validation against `ExtensionManifestV1` (`extension_point == "trainer"`, `protocol_version == "yoao-trainer/v1"`).
    2. `describe` check verifying valid `TrainerCapabilitiesV1` canonical JSON with `CUDA_VISIBLE_DEVICES=""` and `NVIDIA_VISIBLE_DEVICES=void`.
    3. `validate-config` check verifying that an invalid payload returns `valid == False` with non-empty `issues`, and a valid payload returns `valid == True` with `schema_id` and `schema_digest`.
    4. Unless `--skip-train` is passed, `train` happy-path check verifying strict monotonic `seq` in `events.jsonl`, `status == "succeeded"` in `result.json`, matching SHA-256 and byte size for every declared artifact, zero `SUPERVISOR_OWNED_ARTIFACTS` declared, `checkpoint-NNNN.<ext>` naming in `checkpoint.json` when present, `POLICY_DIGEST` and `members_digest` parity in `input-membership.json` when declared, plus a `SIGTERM` cancellation check verifying exit code `130` and `status == "cancelled"` in `result.json`.
    5. When `--preview` is passed, CPU-only `preview` check verifying `report.json` against `PreviewReportV1` with `cuda_initialized == False` and matching output SHA-256 values.
  - [ ] Implement `_scaffold.py` generating a runnable pure-Python CPU trainer package (`pyproject.toml`, `manifest.json`, `demo_trainer.py`, and `tests/test_demo_trainer.py`) using `TrainerPlugin`, `run_trainer_cli`, `EventEmitter`, `CheckpointSlotStore`, and `InputMembership`.
  - [ ] Wire `yoao-plugin trainer check` and `yoao-plugin init --type trainer --example` into `cli.py` so both run in a base `yoao-plugin-sdk` install without `[inference]` extras.
- [ ] **Visible result.** A plugin author runs `yoao-plugin init --type trainer --example ./my-trainer`, installs it in a base Python 3.10+ environment, and runs `yoao-plugin trainer check --manifest ./my-trainer/manifest.json --entrypoint my-trainer` to receive a JSON pass or fail report covering every `yoao-trainer/v1` invariant.
- [ ] **Verification (Unit):**
  - [ ] `pytest packages/python/tests/test_sdk.py packages/python/tests/trainer scripts/test_fetch_contracts.py -q` passes with zero skips.
  - [ ] Negative-control tests prove `check_trainer_plugin` fails when a buggy plugin emits non-monotonic `seq` in `events.jsonl`, declares `manifest.json` in `TrainReceiptV1.artifacts`, writes a mismatched `POLICY_DIGEST` in `input-membership.json`, ignores `SIGTERM` or omits `result.json`, or reports `cuda_initialized=True` in `preview`.
- [ ] **Verification (Live):**
  - [ ] In the `PR-3` worktree inside a fresh venv with only `yoao-plugin-sdk` (zero `[inference]` extras installed), run `yoao-plugin init --type trainer --example /tmp/demo-trainer`, install it with `pip install --no-deps --no-build-isolation /tmp/demo-trainer`, and run `yoao-plugin trainer check --manifest /tmp/demo-trainer/manifest.json --entrypoint demo-trainer --report /tmp/check-report.json`.
  - [ ] Pass predicate requires all three commands to exit `0`, `/tmp/check-report.json` to record `passed: true` across `manifest`, `describe`, `validate_config`, `train`, `cancel`, and `input_membership`, and `yoao-plugin version` and `yoao-plugin validate` to run without importing `httpx` or `fastapi`.
- [ ] **Verification (Perf):**
  - [ ] Metric is `yoao-plugin version` cold CLI startup latency (median of 5 runs) and full `yoao-plugin trainer check` wall time on the generated demo trainer.
  - [ ] Probe runs 5 subprocess invocations of `yoao-plugin version` and 1 timed invocation of `yoao-plugin trainer check` against the generated demo trainer.
  - [ ] Trunk baseline is `143.9 ms` median for `yoao-plugin version`.
  - [ ] Fail threshold triggers if `yoao-plugin version` median exceeds `175 ms` or if full `yoao-plugin trainer check` on the CPU demo trainer exceeds `10.0 s`.

## GATE-A Reproducible `Plugin-SDK` wheel build and cross-repo artifact handoff (maintainer-owned)

- [ ] **Depends on.** `PR-1`, `PR-2`, and `PR-3` merged to `YOAO-Labs/Plugin-SDK:main`.
- [ ] **Review gate.** Maintainer decision gate. Maintainer decides whether to bump `yoao_plugin_sdk/_version.py` before building release artifacts.
- [ ] **Actions:**
  - [ ] Run `python scripts/build-sdk-artifacts.py --revision <Plugin-SDK-main-SHA> --output <staging-dir>` in `YOAO-Labs/Plugin-SDK`.
  - [ ] Record the wheel filename and `SHA256` from `<staging-dir>/manifest.json`.
  - [ ] Publish the artifact bundle and update `YOAO-Labs/YOAO-Plugins` repository variables `YOAO_SDK_MANIFEST` and `YOAO_SDK_MANIFEST_SHA256` so `plugin-trainer.yml` fetches the new wheel.
  - [ ] Stage the verified wheel into local `YOAO-Plugins/.artifacts/python/first-party/` for local development worktrees.

## PR-4 Migrate `plugins/trainer/libreyolo` to `yoao_plugin_sdk.trainer` and delete duplicated code (`YOAO-Plugins`)

- [ ] **Depends on.** `GATE-A`. Branch from `YOAO-Labs/YOAO-Plugins:main` in a dedicated worktree; independent of `PR-5`.
- [ ] **Review gate.** Standard code review with no public CLI contract change.
- [ ] **Files edited:**
  - [ ] `plugins/trainer/libreyolo/Dockerfile` (update `PLUGIN_SDK_SHA256` and `PLUGIN_SDK_WHEEL` if version changed)
  - [ ] `plugins/trainer/libreyolo/pyproject.toml` (update `yoao-plugin-sdk` pin only if version changed in `GATE-A`)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/files.py` (delete)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/errors.py` (delete)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/membership.py` (delete)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/cli.py` (replace argparse boilerplate with `run_trainer_cli`)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/checkpoint.py` (use `CheckpointSlotStore`, `atomic_write_bytes`, and `digest_file`; keep `load_checkpoint_safe(weights_only=True)`, `StrictResumeLogging`, and `aap_resume` checks)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/dataset.py` (delegate `ordered_names`, `validate_source_lineage`, `validate_frozen_manifest_core`, `validate_sample_files`, and `validate_yolo_bbox_label` to SDK; keep LibreYOLO task checks and `write_native_dataset_yaml`)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/evaluation.py` (use SDK `metrics_of`, `hash_source_files`, and `optional_package_version`; keep `strip_metric_aliases`, `libreyolo_report`, and `libreyolo_descriptor`)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/trainer.py` (use SDK `EventEmitter`, `SUPERVISOR_OWNED_ARTIFACTS`, `build_artifact_entry`, `load_dataset_identity`, and `run_with_signal_receipt`)
  - [ ] `plugins/trainer/libreyolo/yoao_trainer_libreyolo/validation.py` (use SDK `normalized` and `validate_value`; keep LibreYOLO schema and cross-field rules)
  - [ ] `plugins/trainer/libreyolo/tests/*.py` (update imports from deleted local helpers to public behavior or SDK imports)
- [ ] **The one change built.** Replace duplicated protocol code in `yoao_trainer_libreyolo` with `yoao_plugin_sdk.trainer` imports and delete `files.py`, `errors.py`, and `membership.py` in the same PR.
- [ ] **Visible result.** `yoao_trainer_libreyolo` shrinks by about 650 to 750 source lines (target at most 2050 lines from the 2704 baseline) while emitting byte-identical `describe`, `validate-config`, `events.jsonl`, `input-membership.json`, `checkpoint.json`, and `result.json` outputs.
- [ ] **Verification (Unit):**
  - [ ] `pytest -q plugins/trainer/libreyolo/tests` passes with all 65 behavioral cases green and zero skips.
  - [ ] AST boundary test confirms `yoao_trainer_libreyolo` imports zero `ultralytics` modules and still enforces `weights_only=True` in `checkpoint.py`.
- [ ] **Verification (Live):**
  - [ ] In the `PR-4` worktree, install the updated `yoao-plugin-sdk` wheel and `plugins/trainer/libreyolo` into a fresh venv, then run `yoao-plugin validate plugins/trainer/libreyolo/manifest.json`, `yoao-plugin trainer check --manifest plugins/trainer/libreyolo/manifest.json --entrypoint yoao-trainer-libreyolo --skip-train`, and diff `yoao-trainer-libreyolo describe` canonical JSON output against the pre-PR baseline captured from `YOAO-Plugins:main`.
  - [ ] Pass predicate requires both CLI commands to exit `0` with `passed: true` and zero diff on `supported_tasks`, `architectures`, `parameter_schema`, `policy_digests`, and `serving_compatibility`.
  - [ ] On a GPU host before publishing updated OCI images, rebuild `plugins/trainer/libreyolo/Dockerfile` with the new `PLUGIN_SDK_SHA256` and execute a 1-epoch RTX 3090 detect train, cancel, resume, and `input-membership.json` verification against `training-supervisor`.
- [ ] **Verification (Perf):**
  - [ ] Metric is `yoao-trainer-libreyolo describe` cold CLI latency (median of 5 runs) and source line count.
  - [ ] Probe runs 5 subprocess invocations of `yoao-trainer-libreyolo describe` plus `wc -l plugins/trainer/libreyolo/yoao_trainer_libreyolo/*.py`.
  - [ ] Trunk baseline is `189.8 ms` median latency and `2704` source lines.
  - [ ] Fail threshold triggers if median latency exceeds `228 ms` (+20%) or if source line count stays above `2150` lines.

## PR-5 Migrate `plugins/trainer/ultralytics` to `yoao_plugin_sdk.trainer` and delete duplicated code (`YOAO-Plugins`)

- [ ] **Depends on.** `GATE-A`. Branch from `YOAO-Labs/YOAO-Plugins:main` in a dedicated worktree; independent of `PR-4`.
- [ ] **Review gate.** Standard code review with no public CLI contract change.
- [ ] **Files edited:**
  - [ ] `plugins/trainer/ultralytics/Dockerfile` (update `PLUGIN_SDK_SHA256` and `PLUGIN_SDK_WHEEL` if version changed)
  - [ ] `plugins/trainer/ultralytics/pyproject.toml` (update `yoao-plugin-sdk` pin only if version changed in `GATE-A`)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/files.py` (delete)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/input_membership.py` (delete)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/__init__.py` (re-export SDK error classes instead of redefining them)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/cli.py` (replace argparse boilerplate with `run_trainer_cli(..., supports_preview=True)`)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/checkpoints.py` (use SDK `CheckpointSlotStore`, `atomic_write_bytes`, and `immutable_copy`; keep `weights_only=False` Ultralytics checkpoint handling and `validate_architecture`)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/dataset.py` (delegate `validate_source_lineage`, `ordered_names`, `validate_sample_files`, and `validate_yolo_bbox_label` to SDK; keep polygon validation and `aap-instance-rle` validation)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/evaluation.py` (use SDK `metrics_of`, `hash_source_files`, and `optional_package_version`; keep `UltralyticsTrainingEvaluation`, `ultralytics_descriptor`, and `ultralytics_report`)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/policies.py` (import `POLICY_DIGEST` from `yoao_plugin_sdk.trainer.membership` instead of recomputing `INPUT_MEMBERSHIP_POLICY_DIGEST`)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/train.py` (use SDK `EventEmitter`, `SUPERVISOR_OWNED_ARTIFACTS`, `build_artifact_entry`, `load_dataset_identity`, and `run_with_signal_receipt`)
  - [ ] `plugins/trainer/ultralytics/yoao_trainer_ultralytics/validation.py` (use SDK `normalized` and `validate_value`; keep Ultralytics schema and cross-field rules)
  - [ ] `plugins/trainer/ultralytics/tests/*.py` (update imports from deleted local helpers to public behavior or SDK imports)
- [ ] **The one change built.** Replace duplicated protocol code in `yoao_trainer_ultralytics` with `yoao_plugin_sdk.trainer` imports and delete `files.py` and `input_membership.py` in the same PR.
- [ ] **Visible result.** `yoao_trainer_ultralytics` shrinks by about 650 to 750 source lines (target at most 4960 lines from the 5609 baseline) while preserving detection, simple-polygon segmentation, `aap-instance-rle`, initialization, and CPU preview behavior unchanged.
- [ ] **Verification (Unit):**
  - [ ] `pytest -q plugins/trainer/ultralytics/tests` passes with all 39 behavioral cases green and zero skips.
  - [ ] Assert `policy_digests()["input_membership"] == "1ad70c6b02157e1b4c33646027309a72e73aaa5eba693e7a5fe52ce8f9bb71b4"`.
- [ ] **Verification (Live):**
  - [ ] In the `PR-5` worktree, install the updated `yoao-plugin-sdk` wheel and `plugins/trainer/ultralytics` into a fresh venv, then run `yoao-plugin validate plugins/trainer/ultralytics/manifest.json`, `yoao-plugin trainer check --manifest plugins/trainer/ultralytics/manifest.json --entrypoint yoao-trainer-ultralytics --skip-train`, and diff `yoao-trainer-ultralytics describe` canonical JSON output against the pre-PR baseline captured from `YOAO-Plugins:main`.
  - [ ] Pass predicate requires both CLI commands to exit `0` with `passed: true` and zero diff on `supported_tasks`, `supported_dataset_contracts`, `architectures`, `parameter_schema`, `policy_digests`, and `serving_compatibility`.
  - [ ] On a GPU host before publishing updated OCI images, rebuild `plugins/trainer/ultralytics/Dockerfile` with the new `PLUGIN_SDK_SHA256` and execute RTX 3090 `yolo11n` detect, `yolo11n-seg` RLE train, and CPU-only `preview` against `training-supervisor`.
- [ ] **Verification (Perf):**
  - [ ] Metric is `yoao-trainer-ultralytics describe` cold CLI latency (median of 5 runs) and source line count.
  - [ ] Probe runs 5 subprocess invocations of `yoao-trainer-ultralytics describe` plus `wc -l plugins/trainer/ultralytics/yoao_trainer_ultralytics/*.py`.
  - [ ] Trunk baseline is `190.3 ms` median latency and `5609` source lines.
  - [ ] Fail threshold triggers if median latency exceeds `228 ms` (+20%) or if source line count stays above `5000` lines.

## PR-6 Enforce `yoao-plugin trainer check` in `YOAO-Plugins` CI (`YOAO-Plugins`)

- [ ] **Depends on.** `PR-4` and `PR-5` merged to `YOAO-Labs/YOAO-Plugins:main`.
- [ ] **Review gate.** Standard code review.
- [ ] **Files edited:**
  - [ ] `.github/workflows/plugin-trainer.yml`
  - [ ] `README.md` (update trainer plugin verification command if listed)
- [ ] **The one change built.** Add `yoao-plugin trainer check --manifest plugins/trainer/<name>/manifest.json --entrypoint yoao-trainer-<name> --skip-train` to both `trainer-ultralytics-python312` and `trainer-libreyolo-python312` CI jobs right after `yoao-plugin validate`.
- [ ] **Visible result.** Every future PR touching either official trainer plugin proves `yoao-trainer/v1` manifest, `describe`, and `validate-config` contract compliance in CI.
- [ ] **Verification (Unit):**
  - [ ] Validate YAML syntax and workflow job naming conventions.
- [ ] **Verification (Live):**
  - [ ] Run the exact shell commands from both CI jobs locally in the `PR-6` worktree inside fresh venvs using the published `yoao-plugin-sdk` wheel.
  - [ ] Pass predicate requires both `yoao-plugin trainer check --skip-train` invocations to exit `0` with `"passed": true` and both plugin `pytest` suites to pass (`65` passed and `39` passed).
- [ ] **Verification (Perf):**
  - [ ] Metric is added CI wall time from `yoao-plugin trainer check --skip-train` per trainer job.
  - [ ] Probe times the new CLI command in both fresh venvs.
  - [ ] Trunk baseline is `0 s` (command did not exist previously); baseline CPU test suites run in `2.26 s` (LibreYOLO) and `1.36 s` (Ultralytics).
  - [ ] Fail threshold triggers if either `yoao-plugin trainer check --skip-train` invocation exceeds `5.0 s`.

## PR-7 Developer documentation and plan closure (`YOAO-Docs`, `Plugin-SDK`, `ai-annotation-platform`, review-gated)

- [ ] **Depends on.** `PR-3`, `PR-4`, `PR-5`, and `PR-6`.
- [ ] **Review gate.** Review-gated. Publishes external developer-facing authoring documentation; waits for operator approval before merge.
- [ ] **Files edited:**
  - [ ] `YOAO-Labs/Plugin-SDK`: `README.md` and `packages/python/README.md`
  - [ ] `YOAO-Labs/YOAO-Docs`: `dev/how-to/trainer-plugins.md` (new authoring guide covering `yoao_plugin_sdk.trainer`, `yoao-plugin init --type trainer --example`, `yoao-plugin trainer check`, and the non-ML dependency boundary) plus cross-links in `dev/concepts/training-datasets.md` and `user-guide/online-learning/{ultralytics,libreyolo}.md`
  - [ ] `YOAO-Labs/ai-annotation-platform`: append `## Outcome` to `docs/plans/1795680269_trainer-sdk-shared-components.md`
- [ ] **The one change built.** Document the shared `yoao_plugin_sdk.trainer` API, example template workflow, offline contract verifier, and exact boundary between SDK helpers and framework-private plugin code.
- [ ] **Visible result.** Third-party trainer authors have one authoritative guide and a verified CLI walkthrough matching both official trainers.
- [ ] **Verification (Unit):**
  - [ ] Run `YOAO-Docs` checks: `node scripts/check-doc-frontmatter.mjs`, `node scripts/check-doc-readability.mjs`, and `node scripts/check-dead-links.mjs`.
  - [ ] Run `ai-annotation-platform` checks: `node scripts/check-doc-version-prefix.mjs --staged` and `git diff --check`.
- [ ] **Verification (Live):**
  - [ ] Run every shell command printed in `dev/how-to/trainer-plugins.md` verbatim inside a clean temporary directory (`init --type trainer --example`, wheel install, and `yoao-plugin trainer check`).
  - [ ] Pass predicate requires every copied command to exit `0` and emit the documented JSON fields.
- [ ] **Verification (Perf):**
  - [ ] Metric is `YOAO-Docs` validation suite runtime and dead-link count.
  - [ ] Probe runs all three `YOAO-Docs` check scripts.
  - [ ] Trunk baseline is `0` dead links and `0` frontmatter or readability errors (measured on `YOAO-Docs` `4d72bdd`).
  - [ ] Fail threshold triggers on any non-zero error or dead link count.

## Outcome

- Landed and pushed commits:
  - `YOAO-Labs/Plugin-SDK` (`main`): `bfb0e91` (`PR-1` P0 modules), `6ff7a88` (`PR-2` P1 dataset, validation, CLI driver), `152f7f1` (`PR-3` `yoao-plugin trainer check` and CPU example template), `bbab0f1` (audit hardening across 6 P1 and 3 P2 findings), `958b8ad` (`PR-7` bilingual README updates), `43fb6ce` (unconditional process-group cleanup in `check.py` for 2-core CI runners). Published release `python-sdk-958b8ade47d7` (built from `958b8ad`, `yoao-plugin-sdk==0.1.0`, zero added runtime dependencies; wheel SHA-256 `50667c6e0c6c118fbb7155d096870bc477c6b1621641fb9b3695fda9f476490c`; manifest SHA-256 `cb1d9b88e7eebf5fa1ff67ea79c1a2c067a7309fb4b30c6808dfcf81002a18ec`).
  - `YOAO-Labs/YOAO-Plugins` (`main`): `e25a000` (`PR-4` LibreYOLO migration, 2704 to 1907 source lines, `-797` lines, 65 passed), `f977175` + `30ec0b3` (`PR-5` Ultralytics migration + audit preview pre-read bound fix, 5609 to 4838 source lines, `-771` lines, 43 passed), `d841e6d` (`PR-6` `plugin-trainer.yml` CI enforcement, `0.816 s` Ultralytics and `0.808 s` LibreYOLO check times), `e0e8a27` (synced `PLUGIN_SDK_SHA256` and staged wheel to `python-sdk-958b8ade47d7`). Configured repository variables `YOAO_SDK_MANIFEST` and `YOAO_SDK_MANIFEST_SHA256`.
  - `YOAO-Labs/YOAO-Docs` (`main`): `05fe97d` (`dev/how-to/trainer-plugins.md`, `.vitepress/navigation/dev.ts`, and cross-link in `dev/concepts/training-datasets.md`; 249 frontmatter, 250 readability, and 257 dead-link checks passed; verbatim walkthrough verified live across all 7 checks).
  - `YOAO-Labs/ai-annotation-platform` (`dev`): plan document and outcome closure only (zero platform runtime changes; JSON Schema parity across all 13 `yoao-trainer/v1` models and `POLICY_DIGEST` `1ad70c6b02157e1b4c33646027309a72e73aaa5eba693e7a5fe52ce8f9bb71b4` verified unchanged).
- Verification evidence:
  - Remote CI:
    - `YOAO-Labs/Plugin-SDK` run `45802186018` (`43fb6ce`): `Python SDK core matrix` (3.10/3.11/3.12), `Python SDK mask utilities matrix` (3.10/3.11/3.12), and `JS SDK Node 20` all passed (pre-existing out-of-scope failure remains on `Python SDK inference and contract artifacts` due to unset `CONTRACT_MANIFEST_URL` repo variable).
    - `YOAO-Labs/YOAO-Plugins` run `45801200418` (`e0e8a27`): `Plugin trainer acceptance` passed both `LibreYOLO Python 3.12 CPU` (15 s) and `Ultralytics Python 3.12 CPU` (26 s).
    - `YOAO-Labs/YOAO-Docs` run `45801069988` (`05fe97d`): `Docs pages` build and deploy passed (pre-existing out-of-scope failure remains on `Docs validation` for `api/guides/_routes.generated.md` drift).
  - Rebuilt OCI RTX 3090 single-GPU smoke (`NVIDIA GeForce RTX 3090`, GPU 1 `GPU-ac5e7f45-7a36-c3f8-1f65-a54f28bfcd31`, ephemeral Postgres/Redis/MinIO/Registry/FastAPI/ML-free `training-supervisor`, 5/5 scenarios passed):
    1. Registry qualification gate (`422` before qualification) + enablement of both rebuilt trainer plugins + ML-free supervisor online.
    2. Ultralytics `yolo11n` detect GPU training (`19.1 s`), container isolation (`user=10001:10001`, `network_mode=none`, `cap_drop=ALL`, `no-new-privileges`, `pids_limit=256`, read-only `/work/input` & `/models`), `input-membership.json`, and model registration.
    3. Ultralytics `yolo11n-seg` instance-RLE (`aap-instance-rle-1`) GPU training (`20.17 s`) and model registration.
    4. Ultralytics container-enforced CPU-only augmentation `preview` (`4.78 s`, `gpu_device_ids=[]`, `cuda_initialized=false`, `761` PNG bytes, GPU idle after preview).
    5. LibreYOLO `libreyolo9t` GPU training (`65.57 s`), cancel after epoch 2, `weights_only=True` `POST /resume`, `libreyolo-native-state-dict-1.0` model return, and model registration.
  - All temporary containers and `yoao-sdk-smoke/*` images retired after evidence capture; GPU 1 confirmed idle (`20 MiB`, `0 %`).
- Release milestone: Not yet determined.
- User documentation: No end-user UI or workflow change.
- Developer documentation: `YOAO-Labs/YOAO-Docs:dev/how-to/trainer-plugins.md`, `YOAO-Labs/Plugin-SDK:README.md`, `YOAO-Labs/Plugin-SDK:README.zh-CN.md`, `YOAO-Labs/Plugin-SDK:packages/python/README.md`, `YOAO-Labs/Plugin-SDK:packages/python/README.zh-CN.md`.
- ADR: Extends existing [ADR-0189](../adr/archive/0189-trainer-extension-point.md) without changing wire protocol or dependency boundaries.
- CHANGELOG: Not applicable in `ai-annotation-platform` (zero core platform runtime change).
- Remaining work: None for this plan. Follow-up if desired: publish a new `Plugin-SDK` release tag from `43fb6ce` when ready to ship the `check.py` CI process-group cleanup in the release wheel (runtime trainer modules are byte-identical to `python-sdk-958b8ade47d7`).
