# Phase 7D ASR-tolerant recall contract — Codex handoff

- Status: partial. A fresh restart smoke is required after this source change.
- DB discipline: no Memory DB was modified; current counts are b=7, c=2.

## Latest real b/c/b Trace verdict

| turn | persona / switch | recall status | retrieved IDs | writes | persona leak |
| --- | --- | --- | --- | --- | --- |
| `97476c63086d` | b / idle | FOUND | 1, 2, 3 | [] | false |
| `c5f90ed07f1f` | c / switch_completed | NOT_APPLICABLE | none | [] | false |
| `0646d32fae92` | b / switch_completed | NOT_APPLICABLE | 1 | [] | false |

The test confirmed persona isolation, correct b binding, no writes, and unchanged DB counts. It does not pass the requested recall contract sequence because C and b-return used ASR-normalized variants which did not satisfy the older generic `recall_requested()` regex. Their query feature hashes also differed, so this is not a stale Trace row.

## Implemented correction

- A passphrase request is explicit recall when it contains `合言葉` and a recall-seeking marker (`教`, `覚`, `前`, `以前`, or `計測`), even if the generic past-reference regex misses it.
- Every explicit recall contract has a deterministic reply; it cannot invoke the LLM. Trace now records `memory.diagnostics.recall_llm_call_count=0`.
- Existing FACT/NOT_FOUND handling is unchanged: b’s labelled fact yields `合言葉は青い灯台だよ。`; c’s zero result yields the short non-invented response.

## Tests

- compileall: PASS.
- Isolated scope/write/fallback/response-contract tests: PASS, including the ASR variant and zero LLM-call invariant.
- Full pytest remains blocked by the broken venv/missing dependencies.

## Required next action

Completely restart from the local project, then run only b → c → b. Require `recall_status=FOUND → NOT_FOUND → FOUND`, IDs `1 → [] → 1`, b/c/b binding, `persona_leak=false`, `memory.write_decisions=[]`, `recall_llm_call_count=0`, and physical counts b=7/c=2. Stop on any mismatch.
