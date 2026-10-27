# Online learning: LibreYOLO training adapter

Status: accepted

The user selected the official LibreYOLO repository and delegated phase confirmation. The initial adapter supports image detection training and verified artifact downloads. Model registration remains capability-gated until a corresponding inference backend passes its own acceptance.

## Fixed contract

Pin official LibreYOLO 1.6.0, commit `809009bc294daa6e75e0976ab7cfdd0057d17d79`, with the existing pinned Torch/CUDA base. Use controlled `LibreYOLO9t.pt` from official model revision `66ac39d1ac9b880076b96ad4674ef9235ca1a15e`, 8,645,036 bytes, SHA256 `b4d7e93f9e0393830fb42e6135c0e3464b2673b05e5ecf4b7f2374ec18e39eb2`. Verify actual bytes and native model loading before enabling its catalog entry.

Reuse the frozen YOLO-format detection package after validating classes, partitions, every label file, and explicit negatives. Do not pass Ultralytics callback names or ignored parameters. The adapter owns framework-specific configuration, native callbacks, metric interpretation, full-epoch checkpoint validation, final weight selection, and evaluation.

Native `precision` aliases represent mAP50-95 and `recall` aliases represent AR100. Report their actual semantics, preserve native values, and explicitly report unavailable per-class evaluation. Reload the exact chosen final weights before validation and record their SHA and evaluation settings.

A resumable checkpoint must preserve optimizer/EMA/epoch/class identity and the historical best weights. Carry a platform-owned extension inside the validated checkpoint so a new attempt retains the earlier best even without a later improvement. Only verified platform checkpoints may enter the native trusted resume loader. Relocate data/project/output paths into the new attempt. Cancellation continues to use the existing process-group supervisor, full-epoch commit, GPU lease and upload recovery protocol.

## Implementation and verification

Add a framework adapter registry with explicit capabilities, frozen options and validation; add the LibreYOLO adapter and pinned dependency; reuse the shared supervisor and artifact lifecycle. Update catalog/UI capability messages so download-only runs cannot enter the Ultralytics model-registration path. Synchronize API schema, user/API/architecture/operations docs, dependency lock and image build inputs.

Verify adapter selection, native argument mapping, negative labels, metric semantics, historical-best resume, cancellation and artifact provenance in bounded tests. Build the isolated image and run real small-sample training on the already authorized single GPU. Exercise cancel/resume, successful artifact downloads and native reload, confirm project publication unchanged, and disable Libre new creation without affecting retained Ultralytics artifacts. Remove owned test resources after final acceptance.

## Rollback

Remove only LibreYOLO's catalog entry to stop new runs. Keep existing experiments, downloads, datasets and the current project model release. No migration is required for the framework adapter itself.

## Acceptance evidence

The isolated browser created native LibreYOLO image-detection experiments using the verified official base. Run `0266b76c-8521-4443-8f7f-ee4471d14f2f` completed two epochs. Real validation exposed the native integer-name-index requirement; the adapter now generates a separate normalized YAML while preserving the frozen package. A longer run exposed OpenCV's 112-thread default against the container limit; OpenCV and Torch thread pools are now explicitly bounded.

Final image `sha256:18bbf015fd301be9afd532e67adc4d2d7eec4620f06e73ac601cfadfdd64c674` ran experiment `2825c8c9-cce6-4179-b1be-8fa4b12ee7ce` on the authorized single GPU. The browser stopped it after six committed epochs and resumed into a second attempt, which completed epoch 30. Native restoration produced no optimizer/EMA/RNG/scaler warnings. All six artifact roles downloaded with matching size and SHA256. Native CPU reload confirmed detection task and ordered classes `car`, `person`; the final checkpoint's historical-best bytes matched the downloaded best weights and evaluation identity `d9259e8c5800c7e06b462de07d61069ddb78e52fe4d990bb7ab1df0db4286231`.

The current project release remained generation 5 and the automatic policy stayed disabled. The 390px browser view had no horizontal overflow or console errors and displayed the native-download-only capability. The experiment's download action was exercised; actual downloaded bytes were independently verified. Tiny synthetic validation data proves execution and provenance, not model quality.

Verification passed: adapter/supervisor tests, 11 final native adapter regressions, catalog capability/frozen-options API regression, model and training UI regressions, Python/web/CSS lint, TypeScript, OpenAPI snapshot check, four SDK contract checks, documentation build, formatting and final diff check. Read-only review closed the resume-before-next-epoch cancellation defect; the verified input checkpoint is committed before native loading, and its regression checks exact bytes and continued resumability. Owned workers registered and dispatched the policy task after the final refresh. Test environments and temporary artifacts are removed after this recorded acceptance.

## Primary references

- [Official stable release](https://github.com/LibreYOLO/libreyolo/releases/tag/v1.6.0)
- [Training implementation](https://github.com/LibreYOLO/libreyolo/blob/809009bc294daa6e75e0976ab7cfdd0057d17d79/libreyolo/models/yolo9/model.py)
- [Native trainer and resume](https://github.com/LibreYOLO/libreyolo/blob/809009bc294daa6e75e0976ab7cfdd0057d17d79/libreyolo/training/trainer.py)
- [Dataset format](https://www.libreyolo.com/docs/train/datasets)
- [Metric semantics](https://www.libreyolo.com/docs/train/validation)

## Outcome

- Delivered scope: Delivered the qualified native LibreYOLO detection-training adapter, full-epoch cancellation/resume and verified artifact downloads. Managed inference was a later independently qualified scope.
- Completion evidence: the existing `Acceptance evidence` section above records implementation and verification. Archival on 2026-10-22 reuses that record; application tests and live acceptance were not rerun for this documentation move.
- Enduring documentation: [libreyolo.md](../../../docs-site/user-guide/online-learning/libreyolo.md), [training-datasets.md](../../../docs-site/dev/concepts/training-datasets.md); user impact is recorded in `CHANGELOG.md`.
- Release milestone: no new milestone or version is assigned by this archival.
- Later inference qualification: [T13 managed model return](1792036132_training-libreyolo-model-return.md) records that separate completed scope; this phase's original download-only evidence remains unchanged.
