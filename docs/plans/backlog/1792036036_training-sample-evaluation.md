# T08 — Frozen sample evaluation and error review

> Status: trigger-gated research-draft
> Priority: after data and comparison basics
> Depends on: [T04](../1792035920_training-data-insights.md), [T05](1792036036_training-experiment-comparison.md)
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

Managers inspect missed, extra, confused, and poorly localized detections on a fixed sample set, compare qualified candidates, and open the source task for human review. Promotion requires a qualified evaluator with explicit matching/threshold semantics and a deployed managed model supporting identity-verified inference.

Current [installation trial](../../../apps/api/app/services/trained_models.py) runs a fixed validation image. Reuse [immutable model identity](../../../apps/api/app/db/models/trained_model.py), [controlled routing](../../../apps/api/app/services/ml_routing/router.py), frozen data reads, and [Workbench navigation](../../../apps/web/src/utils/workbenchNavigation.ts). Do not implement this by launching preannotation that writes project predictions or annotations.

## Domain and execution contract

- Add an immutable evaluation specification and durable EvaluationRun/attempt records: model/deployment and exact weight SHA, frozen sample/label manifest, class bindings, split role, evaluator/version, IoU/confidence/matching rules, and request identity. Use existing AsyncJob presentation without making it authoritative.
- Start with qualified Ultralytics managed deployments on existing inference resources. Use existing inference admission and strict model references; do not borrow a training GPU outside its resource contract. LibreYOLO enters only after T13. Evaluation orchestration can use existing Celery; no new always-on service is required.
- Version the evaluator and preserve native/protocol settings. Finalize bounded per-sample predictions, one-to-one matching results, class confusion, and aggregate metrics as verified immutable artifacts. Reference deletion must be checked before removing source storage objects.
- Validate response model identity and class indexes on every batch. Support cancellation and recoverable attempt ownership without publishing partial reports as completed evaluation. A failed sample is counted and prevents an unqualified aggregate from masquerading as complete.
- Mark validation data as used for model selection. Independently held-out test data is explicit and must not become an automatic tuning target. No test set is silently synthesized from the same validation data.

```text
Frozen sample/label manifest + installed model -> EvaluationRun
  -> authorized orchestrator -> managed inference admission -> identity-checked outputs
  -> versioned evaluator -> immutable report -> gallery -> source Workbench link
```

## Review UX and acceptance

Provide filters for class, false positive/negative, confusion, confidence, and localization. Display frozen truth and model output with threshold controls clearly marked as view recomputation or a new evaluation specification. Two-candidate views require the same benchmark/evaluator settings.

Opening Workbench resolves the current task and media identity using existing navigation helpers. Show when current data differs from frozen evidence or the source task was deleted. Human edits use normal editing/review guards and produce a later data version; they never alter historical evaluation truth or automatically accept model output.

Expect more than eight files and additive evaluation/reference migrations. Verify duplicate detections and one-to-one matching, empty negatives, class confusion, threshold boundaries, sample failures, stale/foreign deployment responses, corruption, pagination, authority revocation, cancellation, and retained-reference deletion guards. Use a hand-checkable native fixture and inspect actual gallery-to-Workbench navigation, mobile layout, and refresh.

The fragile assumption is that a reported error means incorrect truth; preserve model output as evidence, with human judgment separate. The minimal option is a bounded gallery from verified native evaluation artifacts without rerunning legacy candidates. Rollback stops new evaluations and retains completed reports, models, source versions, and normal annotation. Apply shared verification, documentation, cleanup, and promotion rules.
