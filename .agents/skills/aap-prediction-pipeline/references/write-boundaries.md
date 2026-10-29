# Prediction inputs and final writes

## Discover the implemented path

Trace the current checkout from the initiating route to input capture, execution, result qualification, candidate persistence and acceptance. On the integrated API path, relevant owners include `services/prediction.py`, `reference_native_predictions.py` and `video_prediction_acceptance.py`. Module branches may instead expose `labeling_write_scope`, `labeling_write_guard`, `SecondaryInferenceWritePort`, and accepted-input contracts. Discover these first; do not introduce absent module interfaces as part of a routine pipeline fix.

## Inputs are facts, not reusable authorization

- Preserve the exact original/reference content, selected task/frame, logical dimensions, geometry transform, and model/deployment provenance across inference. Do not replace an expired receipt by resolving today's file or defaulting a missing actor/execution scope.
- Keep public provenance separate from private validation receipts. Metadata copied into a result is not proof that the result came from the qualified input. Preserve the current receipt/anchor validation and persisted qualification before writing native masks or video results.
- A source or permission change during a long model call must be checked at the final write boundary. An earlier HTTP authorization or captured immutable scope does not authorize a later mutation.
- Execution results do not own task, annotation or Job transactions. The final labeling/domain owner rechecks authority, locks, task/batch state, source binding and annotation version in its original transaction. A helper must not commit, open an independent write session, or reconstruct a scope from whatever is currently selected.

## Separated module path, only where implemented

- Persist the declared execution scope in the durable Job before fan-out; distinguish unknown from intentionally actorless execution. Re-read original user/Key/project access, exact Job/task identity and real terminal state through the existing write guard.
- Inference returns immutable geometry/attribute facts. A `SecondaryInferenceWritePort` binds those facts to the original transaction and annotation owner; that owner alone applies attributes/children and advances changed versions. Do not restore private-table access or hide a network transaction inside that port.
- Keep cooperation/cancel intent distinct from the durable terminal fence. Late success, an old failure signal, or a replaced attempt cannot reopen a terminal Job or publish into a new task/session.

## Verification boundary

Use the existing tests for the changed route/worker and final owner, then exercise the real operation. Select the relevant race: source changed during inference, revoked original Key, foreign edit lock, terminal Job, changed annotation version, replaced attempt, or assignment after bulk preannotation. Include native-mask/video qualification only when those paths changed. Do not turn one payload or storage fix into an unrelated Tracker/session/review redesign.

Current architecture: [prediction pipeline](../../../../docs-site/dev/concepts/prediction-pipeline.md). Environment and worker refresh: [aap-runtime](../../aap-runtime/SKILL.md). Module isolation and physical inference lifecycle: [aap-backend-boundaries](../../aap-backend-boundaries/SKILL.md).
