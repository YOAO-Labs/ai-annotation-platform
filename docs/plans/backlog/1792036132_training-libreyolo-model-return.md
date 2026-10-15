# T13 — Qualified LibreYOLO managed model return

> Status: trigger-gated research-draft
> Priority: independent capability expansion
> Depends on: existing managed model and deployment contracts
> Parent: [experience roadmap](1792035920_online-learning-experience-roadmap.md)

## Outcome and promotion trigger

A verified LibreYOLO image-detection experiment can become a managed candidate, pass native inference checks, and be manually published or rolled back with the same provenance guarantees as existing qualified models.

Promotion requires native CPU load/inference of the exact retained weight format, verified ordered classes and output semantics, and a dependency-compatible inference runtime. Current [registration](../../../apps/api/app/services/trained_models.py) and [managed backend metadata](../../../apps/yolo-backend/managed_models.py) intentionally restrict this path to Ultralytics. [Training qualification](../1791983290_online-learning-phase-e.md) does not establish inference compatibility.

## Chosen approach and boundaries

- Prefer a framework adapter within the existing controlled inference backend, reusing model identity, artifact verification, installation receipts, pool admission, and routing. Add pinned LibreYOLO dependencies only after checking Torch/native compatibility in that backend image.
- If dependency constraints prevent a shared runtime, stop promotion and write the concrete separate-backend deployment tradeoff for maintainer selection. A second always-on service is not implicitly authorized by this draft.
- Add native LibreYOLO loading and result normalization into the shared detection contract: geometry, coordinate space, class indexes, confidence, image dimensions, and actual model identity. Do not run a LibreYOLO checkpoint through the Ultralytics loader or assume similarly named result fields have identical meaning.
- Qualify framework/version/model-family/task, exact weights SHA/size, ordered classes, and runtime identity. Installation receipts and cache keys include framework and immutable artifact identity; concurrent model loads cannot mix class metadata.
- Preserve exact-instance/version routing and project bindings. Missing or unhealthy compatible deployments fail explicitly; no fallback to a different framework, built-in model, or merely available service-pool member.
- Expand registration capability only after loader, output, receipt, trial, and routing checks pass. The project UI then exposes the same manual candidate/deploy/trial/publish/rollback flow. Existing download-only experiments remain readable and downloadable.
- Keep final training evaluation semantics intact. Per-class/native metric gaps stay unavailable. T05/T08 permit comparisons only under a qualified shared evaluator, not by relabeling native precision/recall aliases.

```text
Verified LibreYOLO weights -> managed native loader -> identity/classes receipt
  -> fixed-sample trial -> explicit manual release -> exact deployment routing
  -> normalized predictions + provenance -> normal human review
```

## Scope, acceptance, and rollback

This is more than eight files across backend dependency/image inputs, managed metadata/loaders/results, capability reporting, platform registration, routing tests, UI, and docs. Existing backend authentication is reused; no new user API key or external account. If any new deployment variable becomes necessary, promotion must list it and its standard generated documentation explicitly.

Verify native reload, two models with different class orders, cache isolation, malformed/foreign weights, exact SHA checks, output normalization, stale/foreign receipts, pool member mismatch, permission revocation, and unchanged Ultralytics behavior. Real acceptance includes a bounded registered LibreYOLO model, fixed-sample inference, human-controlled publication, prediction provenance, and rollback to the prior verified release; use only authorized resources.

The fragile assumption is that training and inference native artifacts are interchangeable. Gate each model-family/artifact format independently. The minimal option keeps native weight downloads for external inference. Rollback disables new LibreYOLO registrations/installs; migrate or explicitly roll back active bindings before disabling serving. Preserve historical weights, deployments, releases, predictions, and training experiments. Apply shared verification, cleanup, documentation, and promotion rules.
