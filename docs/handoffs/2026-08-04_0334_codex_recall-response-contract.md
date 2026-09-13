# Phase 7D recall evidence and response contract — Codex handoff

- Status: partial. Restart smoke has not been run from this source.
- DB discipline: all b/c inspection and direct retrieval probes used SQLite `mode=ro`; no DB record was changed.

## Read-only findings

Increment rows:

| DB / ID | safe summary | kind | retrieval keys | created_at | source event / origin turn | named test terms |
| --- | --- | --- | --- | --- | --- | --- |
| b / 6 | `前に覚えてもらったことを覚えてる` | episode | [] | 1785779875.6428823 | `612bb59b3bf4` / same | none |
| b / 7 | `前に覚えてもらった計測用の合言葉を教えて` | episode | [] | 1785780000.3378587 | `9ac9366df0fb` / same | none |
| c / 2 | `前に覚えてもらった計測用の合言葉を教えて` | episode | [] | 1785780015.7017665 | `c41a27339c8f` / same | none |

The checked terms `量子猫の箱`, `量子跳躍の果てに咲く花`, `青い灯台`, and `合言葉` were absent from those three increments. Physical counts remain b=7, c=2; both DBs have zero episode embeddings.

Before the repair, invoking the actual semantic-to-lexical fallback with a read-only store selected b ID 7 and c ID 2 at lexical score 1.0. Those are stored questions, not facts.

After the repair, the same read-only invocation selects b ID 1 (`青い灯台`) at 0.3333 (then normal b facts), and c selects none. No writes or touches occurred.

## Implemented contract

- Both semantic and lexical retrieval skip records whose content itself is an explicit recall request.
- `Mind.recall_response_contract()` applies only to explicit recall requests after retrieval:
  - no selected fact → `recall_status=NOT_FOUND`, deterministic short "覚えていない" reply;
  - selected `合言葉は…` fact → `recall_status=FOUND`, deterministic `合言葉は青い灯台だよ。` reply.
- Pipeline uses that reply instead of invoking the LLM, before any TTS token. This prevents replacement by `量子猫の箱` or another invented passphrase.
- Trace emits `memory.diagnostics.recall_status`; it does not emit fact text.

## Tests

- `python -m compileall -q neuro_voice tests/test_memory_retrieval_wiring.py`: PASS.
- Isolated tests: strict persona scope, provenance, write gate, missing-vector lexical fallback, question write gate, response contract and safe Trace diagnostics: PASS.
- Full pytest remains blocked by the broken venv/missing test dependencies.

## Required next action

1. Fully restart AItuber once from this local project.
2. Verify `memory.write_enabled=false` plus the existing retrieval/scope/turn/trace flags.
3. Run only b → c → b with the passphrase question. Expect b direct `青い灯台`, c direct `覚えていない`, b direct `青い灯台`; each Trace has `memory.write_decisions=[]` and `recall_status=FOUND/NOT_FOUND/FOUND`.
4. Verify b=7 and c=2 remain unchanged for this current contaminated baseline. Do not delete the increments without explicit authorization.
