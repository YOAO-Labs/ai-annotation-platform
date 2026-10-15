# T07 — Training observability and failure guidance

> Status: research-draft
> Priority: independent reliability branch
> Depends on: existing executor ownership and attempt fencing
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and existing owners

Managers can see whether training is computing, waiting on data, evaluating, or uploading, and understand the next valid action after failure. This extends current stages and epoch curves without creating another execution state machine.

Reuse [executor worker](../../../apps/training-executor/training_executor/worker.py), [runner callbacks](../../../apps/training-executor/training_executor/train.py), [event/heartbeat schema](../../../apps/api/app/schemas/training_run.py), [training services](../../../apps/api/app/services/training_runs.py), [event consumers](../../../apps/web/src/pages/Training/useTrainingEvents.ts), and [monitor](../../../apps/web/src/pages/Training/TrainingRunMonitor.tsx). Runtime identity currently describes static environment facts, not live utilization.

## Telemetry and log contract

- Add a separate typed telemetry stream keyed by run, attempt, executor boot, fence, and sample cursor. It cannot advance execution status or release a lease. Reject stale attempts and duplicate/out-of-order samples deterministically.
- Sample allocated-resource memory, process/physical GPU utilization where attributable, throughput, and elapsed phase durations. Label device-wide values separately from process-owned values. Unsupported measurements remain unavailable.
- Start with a five-second sampling cadence and a bounded recent window; promotion fixes database retention and final-summary artifact limits after measuring volume. Raw native metric names are preserved separately from normalized display units.
- Expose bounded, redacted live-log pages with a cursor independent of epoch events. Redact signed object URLs, executor credentials, and unintended host paths before persistence/display. Rendering escapes content and handles partial lines/large messages.
- Persist structured failure stage/code when supported: data/input mismatch, incompatible config/runtime, OOM, native failure, upload failure, or execution lost. Unknown errors remain unknown with a sanitized diagnostic; do not guess a root cause from one substring.
- Show elapsed time immediately. Show remaining-time estimates only after comparable observed epochs, with a range and sample basis; separate training estimate from evaluation/upload. Resume and early stopping must not imply a guaranteed finish time.

## Recovery UX

- OOM offers clone-to-draft and highlights batch/size settings; it never silently changes frozen parameters.
- Resume appears only for the existing verified stopped checkpoint and compatible immutable inputs. Changed parameters or data create a new experiment.
- Upload recovery is handled by the existing executor journal and artifact protocol; the page must not offer retraining as an upload retry.
- Cancelling remains a request until acknowledged process exit. Lost execution retains its resource ownership and shows reconciliation status; telemetry failure alone never frees it.

```text
Owned native process -> telemetry/log spool -> fenced bounded ingestion -> monitor
Training domain -------------------------------------> status/recovery action eligibility
```

## Scope, verification, and rollback

Candidate changes span telemetry/log persistence, public/internal schemas, executor sampling, adapters, and monitoring. More than eight files are likely; a resource sampler dependency is added only if existing system/native interfaces cannot provide the required data. No external monitoring account or new service is required.

Verify no-device/no-permission sampling, zero/invalid values, stalled callbacks, cursor replay, bounded storage/response sizes, redaction, stale fences, restart recovery, long logs, OOM guidance, cancellation/lost states, and measured ETA changes. Native acceptance checks actual GPU identity and ensures sampling does not materially delay heartbeat or epochs. Browser tests cover missing data, reconnect, attempt switching, and mobile graphs.

The fragile assumption is that host-level measurements describe this process; attribution labels and unavailable states prevent misleading claims. The minimal option adds structured failure guidance and actual phase elapsed times. Rollback stops telemetry/log ingestion and hides its views while preserving core heartbeats, epochs, attempt status, and historical summaries. Apply shared promotion, documentation, and cleanup rules.
