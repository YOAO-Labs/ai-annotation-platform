# Cross-agent session audit

Snapshot: 2026-10-28, primary checkout `8a383acc4b2eb5d5142804f85c579789fce01358`. This is skill-maintenance evidence, not a runtime recipe or new product acceptance result. Only `aap-*` skills and their discovery links are in the authorized write scope.

## Coverage and indexes

- [Session inventory](session-inventory.tsv): 1,130 metadata rows. A row identifies a retained transcript file, database session, or summary; it is **not** an independent human conversation count. No prompts, titles, tool output, credentials or raw message bodies are copied.
- [Branch/worktree snapshot](branch-worktrees.tsv): all 27 local branches, three remote-tracking branches and the symbolic `origin/HEAD` ref; 25 registered worktrees. No fetch, checkout, merge, pruning or worktree deletion was performed.
- Recorded `cwd`, Git origin/branch, parent lineage and database indexes establish historical project membership. A directory's present existence or Git registration is not a filter. Both old desktop siblings and removed Orca/Codex/Claude worktrees remain in the inventory.
- Indexes use `~` for the local home prefix. Branch names and historical directory spelling are preserved. A missing recorded branch remains unknown; zero exact branch-name matches do not establish that no session exists.

### Retained evidence

| Store | Project evidence | Examination and limits |
| --- | --- | --- |
| Codex | 801 physical files / 800 unique thread IDs; 1,304,989 project records | Stream-parsed all 838 retained rollout files, including 686 compressed files; matched session/turn cwd, origin, indexed metadata and parent lineage. Zero malformed records or decompression errors across the whole scan. Project events span July 9–October 26. |
| Claude Code | 44 JSONL files; 19 top-level, 25 nested; 23,218 records | Full-file parsing, plus selective correction/outcome review. Seven project-directory aliases; events span July 9–September 18. Top-level includes dispatched workers and automated reviews. |
| OMP | Seven historical roots and 165 child/nested logs; 92,329 records | Full-file parsing, plus selective original-user and worker-outcome review. October 26–28; current audit root/children excluded. Live logs were read per file, not as an atomic snapshot. |
| Cursor | Seven transcript exports; 352 records | Full-file parsing and selected September 10–11 evidence. Seven same-ID SQLite blob stores were inventoried, not independently decoded or counted again. |
| OpenCode | 61 database sessions; 44 null-parent, 17 children | Read-only session/message queries and selective correction/outcome review, September 14–October 20; 29 recorded directories. Null parent can still be dispatched work. |
| ZCode / GLM | 34 database sessions; six roots, 28 children | Read-only metadata/message queries, July 14–21. Root user-role records include 313 explicitly synthetic records; continuations are not new human requests. |
| Antigravity | 11 summary rows; eight roots, three children | Summary/preview evidence only, October 12–19 apart from one empty zero-date record. Full conversations were not recovered; local conversation/brain directories were empty. |

Claude, OMP and Cursor JSONL parsing also found zero malformed records. Semantic review was selective: parsing every record does not mean every tool result was manually audited. The SQLite stores were queried read-only, not rewritten.

The OMP archive API exposed the primary, `dataset_opt` and `feat-backend-opt` roots; its history index also contains drafts/commands and two IDs without a raw root. Those are not additional recovered conversations. Orca's two journal entries point to Codex and are not counted again. The 38 legacy Claude/OpenCode transcript exports belong to RF-DETR and were excluded.

The earlier September 6 audit describes a Mac-local corpus not present on this machine. It remains historical evidence, not an addition to today's file counts. Deleted histories, other machines, unavailable exports, opaque Cursor blobs and Antigravity full messages remain outside recoverable coverage.

### Historical worktrees and deduplication

Codex records 28 cwd values and 41 branch names. Indexed origins classify 139 interactive-origin threads, 647 spawned threads, 12 guardians and two reviews; origin classification does not prove a distinct human request. One thread, `01a0d224-f9c8-7312-a9a1-875d9112781a`, has both September and resumed October physical files; both were parsed and retained, but the thread is counted once. In 302 files, inherited `session_meta` records contain multiple IDs. The inventory uses the actual rollout thread prefix reconciled with SQLite, not the first sorted inherited ID; an extra resumed filename suffix is not a new thread.

Recovered aliases include desktop `-E2E_opt`, `-production_opt`, `-workbench_260911`, `-workbench_opt260914`, `-feedback_260910`, old Claude parity/dd-* worktrees, the Codex `0dc6` worktree, and Orca `online_learning_closeloop` / `dev_docs_opt`. OpenCode contributes additional removed desktop siblings and `worktree-agent-opt-*` paths. Exact aliases are queryable in the TSV rather than repeated as a second directory catalog.

OMP child headers retain parent-session launch cwd; execution in `/tmp/aap-libreyolo-1793158678/*` is additionally evidenced by delegated work and the Git worktree snapshot. Do not rewrite launch cwd as execution cwd. Current plugin branch source is not present on main and is not treated as main's installed runtime.

Claude `b9443e6d-37ef-4f82-a684-3c907742da79` repeats the same human-message UUID/time at lines 1319 and 3994. Inherited prompts, compactions, queue records, child output, synthetic messages, slash commands and duplicate exports are evidence copies, not independent recurrences.

## Evidence promoted into skills

Line numbers below refer to original or decompressed JSONL, not filtered output. Full file locators are in the inventory. Database sequences identify message rows in the local stores. Historical pass claims explain a rule; none certify the current application runtime.

### Dataset/storage and task scope

- Codex `01a162dd-bc9d-78d2-a60f-f37cccac3c29`, lines 98290/98532/98602: the user challenged a storage epic drifting into Tracker/session/review work. Lines 117604/123533 distinguish an unrelated 10k-item training prerequisite from an explicitly authorized bug that actually blocked the feature. Lines 163233/164999 reject repetitive rollback drills without a changed rollback boundary.
- The same session, lines 111690/143260/169916, separates current-byte baseline, saved old locations, new-write cutover, local evidence and production migration. OMP `01a17b29-352d-72d6-8d26-552c2143b02a`, lines 178/366/616, records post-upgrade storage errors and location-backfill/full-byte-verification recovery rather than re-import.
- Promoted into `aap-dataset-storage`; corroborated against `dataset-sources-and-sync.md`, `original-access-and-retention.md`, `original_sources.py` and `managed_original_creation.py`. No historical rollback choice became a universal new implementation requirement.

### Online learning and physical execution

- Codex `01a15815-86d5-7cb2-a291-ac7710023b63`, lines 2738/2982/3585, requires qualification of the changed segmentation path rather than borrowing old YOLO lifecycle evidence. Lines 12881/12888/13790 separate training, serving and evaluation runtime identity and explicitly qualified compatibility.
- OMP `01a1764f-1123-77ba-bd38-3e11f6c72ceb`, lines 530–533, reports frozen native loader-member receipts; a node cache remained conditional on a measured transfer bottleneck. Codex `01a15206-bbf3-7ac2-b24a-cadbe98147c6`, lines 595/959, corrects the assumption that healthy inference also means the independent trainer is ready.
- Promoted into `aap-online-learning` and runtime guidance; checked against `training-datasets.md`, `training_runs.py` and `training_contract/inference_runtime.py`. Training support, model return, fixed-sample trial, publication and CUDA qualification remain separate claims.

### Module, plugin and final-write ownership

- OMP `01a17b34-be92-7769-9f12-460bf975b8c4`, lines 7/6377, distinguishes a main merge from completed module/plugin boundaries. Lines 6577/6649/6754/6833 retain the explicit train-to-native-plugin/GPU request rather than substituting CPU evidence.
- Its children `GpuQuiescenceRepair:193`, `OperatorRecoveryRepair:282`, `NativeLoaderRepair:78` and `DetectionSDK:859` describe native drain, launch-bound recovery, narrow checkpoint conversion and author hooks. These are worker handoffs; several explicitly ran no checks. They are not fresh acceptance passes or additional human requests.
- Promoted into branch-gated `aap-backend-boundaries` and prediction write references. Validated against the actual `feat/backend-opt` checkout's `apps/inference/README.md`, `plugin_installation_observation.py`, `labeling_write_scope.py` and routing documentation; main's existing prediction receipt path remains separately described. No unmerged API or generic relaxed checkpoint-loading rule was installed on main.

### Workbench ownership and diagnosis

- OpenCode `ses_f5cfc1ae4ffek9cXy3EKJEeG9D`, sequences 1795/1946/2595: repeated Pinyin first-character failure, interrupted composition and caret ownership. `ses_f581ed065ffeQ40h4pngT7bsbF`, sequences 2670/2964: a suppressed native prompt meant the batch action never sent a request.
- Claude `a55024d4-83b0-4e75-8696-d9b5270b112c`, lines 3/41–46/81, distinguishes single-track entry semantics from multi-target operations; `e4110d99-f462-4fac-a2ad-2e2785777c1f:3` records non-bbox action parity. Cursor `eb36f54c-b743-424c-bd1e-5ff367585ddf:24,274–275` records named presets separately from automatic layout.
- ZCode `sess_ece173f3-5e2a-4c92-a02a-8dc2f2460dba`, sequences 940/964–977, corrects symptom/toast treatment when ML containers were stopped. OMP `01a17768-55e1-7143-8f2c-a03429eb8b80:231–235,260` distinguishes localhost preview success from human reachability.
- Promoted into Workbench lifecycle and runtime diagnostics; checked against `CommentInput.tsx`, `useWorkbenchNamedPresets.ts`, `ReviewPage.tsx` and `api/originals.ts`. Existing media/release/SDK skills already cover their observed boundaries and were not rewritten for novelty.

### Renderer evidence

- Codex child `01a1395a-a96f-7c41-8439-3c00bf02ebe8:125` distinguishes display XYZ from raw LiDAR/calibration/sweep input; its copied parent prompt is not another request. `01a11ef1-9805-7860-88d5-9fe2f9ea08b2:224,750,1022,1079` separates acquisition ground truth, camera/time denominators and asynchronously stale geometry/metrics.
- Promoted as conditional evidence principles in `aap-renderer-validation`, not as new reconstruction APIs, thresholds or main-branch qualification. Current import/source contracts were checked; historical experimental reconstruction code is not assumed present.

## Maintenance method

For a future audit, inventory current refs/worktrees **and** agent-store metadata before filtering by project. Include compressed rollouts and historical cwd/parent lineage, reconcile indexed paths after compression, and stream oversized members instead of treating a reader extraction limit as corruption. Keep file, thread, message and human-request counts distinct. Inspect original corrections and outcomes, then corroborate rules against the target checkout. Preserve gaps and uncertainty; only promote reusable project-specific decisions within the user's authorized skill scope.

## Verification of skill changes

- All ten AAP entrypoints pass the bundled skill-creator validator, name/directory checks and the 100-line limit; changed/new entrypoints remain 24–55 lines. All ten Claude discovery symlinks resolve to their repository-owned AAP skills.
- All 44 relative links checked across AAP entrypoints and changed references resolve. The session catalog validates 1,130 rows, per-store counts, 800 distinct Codex thread IDs, and absence of the checked credential/DSN patterns.
- A stateless six-scenario dry-run using only the first 30 entrypoint lines retained the intended storage scope, missing-branch gate, GPU evidence boundary, IME ownership, cancellation uncertainty and deleted-worktree coverage. This is instruction evaluation, not application/runtime qualification.
- Prettier and `git diff --check` pass for the skill documentation changes. No application code, dependency, data, deployment or non-AAP skill changed; no application tests, browser acceptance or service restarts were required or claimed.
