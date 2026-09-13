# Phase 7D retrieval-only smoke guard — Codex handoff

- Status: partial. Do not run further unscripted Memory conversations before the one controlled b/c/b check.
- Scope: Memory retrieval path and test-only write suppression only. No DB row was changed by this work.

## Observed failure

The prior real-turn Trace bound the right persona (`b` then `c`), showed no cross-persona result, and had `candidate_count=0`. The cause was not a persona leak: the enabled embedder selected semantic recall, but the b/c persona databases had no stored episode embeddings. `_recall_semantic()` returned no candidates instead of using the existing scoped lexical reader. The former `RANK_BELOW_THRESHOLD` label therefore did not identify the real stage.

The earlier test was not read-only: Trace shows episode writes. Current physical episode counts are `mind_b.db=7` and `mind_c.db=2`, rather than the intended test baseline 5/1. Do not silently delete or restore those rows. The user must explicitly authorize any recovery if 5/1 is a hard precondition.

## Implemented

- `Mind._recall_semantic()` now uses `_recall_keyword()` when the scoped embedding index is empty. It retains the strict bound persona filter.
- `memory.write_enabled` defaults to `true`; config sets it to `false` for this controlled test.
- When false, `EpisodicMemoryService.commit()` returns no decisions and never calls a mutation path. Memory lookup also skips `touch()`, and transcript persistence is suppressed. Retrieval remains on.
- Trace reports lexical fallback via the existing lexical count and `embedding_status_counts.MISSING`; no text is logged.
- 🩺 smoke settings display `memory.write_enabled` with its retrieval-only explanation.

## Verification

- `python -m compileall -q neuro_voice`: PASS using bundled runtime.
- Isolated temporary-DB checks: PASS for strict scope, provenance, question write gate, independent write gate, lexical fallback with an enabled fake embedder and no vectors, and privacy-safe Trace diagnostics.
- Full pytest is blocked: project venv base interpreter is broken; bundled runtime lacks `pytest` and `PyYAML`. Direct config loading is consequently also unavailable in that runtime. A system Python path exists but executable launch was access-denied.

## Required next action

1. Fully exit AItuber and start it once from the local project.
2. Verify `memory.retrieval_enabled=true`, `memory.write_enabled=false`, `persona_scope_enabled=true`, `turn_frame_enabled=true`, and `cognition.trace.enabled=true`.
3. Do only b → c → b with the fixed passphrase question. Do not include neuro.
4. Read Trace and counts. Expected per turn: `memory.write_decisions=[]`; b retrieval is bound b; c has zero b sources and `persona_leak=false`; b return retrieves b again.
5. Stop on any mismatch. Do not advance Phase 7D or change Memory DBs.
