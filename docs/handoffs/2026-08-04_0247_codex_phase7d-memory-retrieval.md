# Phase 7D Memory retrieval/write provenance repair — Codex handoff

- Status: partial. Source and isolated tests are ready; a full app restart is needed before any new smoke result is trusted.
- No existing Memory DB record was modified, restored, or deleted.

## Confirmed root cause

The local `VoicePipeline._inject_mind` path called `Mind.build_context(..., include_recall=False)`. This skipped the only long-term recall invocation before response generation. The former Trace used `Mind.last_recall`, which could therefore be empty or stale rather than an actual result for the turn.

## Changes

- Local context now invokes recall (`include_recall=True`) inside the pre-existing bounded context deadline.
- `MemoryStore.all_texts()` and `all_embeddings()` now apply the strict bound persona filter, matching `episodes()` behavior.
- `CognitiveTrace.memory.diagnostics` adds result/reason, query feature hash, bound store identifier/hash, stage counts, rejection codes, and embedding status counts. No query/body is recorded.
- A context/store persona mismatch fails closed with `PERSONA_MISMATCH` rather than falling back.
- Question-shaped input cannot produce a preference candidate.
- Local and Discord completion both pass their turn event ID into new episode candidates. New episode metadata includes `origin_turn_id`, `source_event_ids`, `source_role`, `source_person_id`, `persona_id`, and `persona_epoch`.
- Legacy IDs 203–205 are intentionally untouched; they require a separate archive/backfill decision, not a silent mutation.

## Tests

- `compileall`: PASS for changed modules and the added test module.
- Isolated temporary-DB check: strict b filter, provenance fields, question gate, and Trace diagnostics: PASS.
- Project pytest could not run because the project venv points to a missing base interpreter and bundled Python lacks the required test dependencies.

## Required next action

1. Fully exit and restart AItuber once so it loads this source.
2. Run only controlled b → c → b retrieval checks with memory writing disabled for the check turns; do not add new memories.
3. Inspect the new Trace diagnostics. Expected b result: `RETRIEVED`, bound persona `b`, nonzero selected count. Expected c: zero b results and `persona_leak=false`. Expected b-return: b results again.
4. Run one question-only check. It must show zero write decisions and no new DB row.
5. Stop on any mismatch; do not migrate/archive IDs 203–205 or advance phases.

## Remaining risks

- The context deadline may produce `TIMEOUT` under a cold embedding model; the new Trace will distinguish that from trigger/scope/ranking failures.
- Full transport parity for normal Discord Trace emission remains outside the prior Trace-output task, though Discord writes now receive source event provenance through `close_turn`.
