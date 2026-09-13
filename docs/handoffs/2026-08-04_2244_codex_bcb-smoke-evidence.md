# Phase 7D b/c/b smoke evidence — Codex handoff

## Accepted real-session result

Latest AItuber process started 2026-08-04 22:09:21 JST. Ignore older IDs `97476c63086d`, `c5f90ed07f1f`, and `0646d32fae92`.

| at JST | turn ID | active / bound persona | switch | recall | retrieved candidates | answer evidence | LLM calls | writes | leak |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 22:10:29 | `12ee34199cbf` | b / b | idle | FOUND | [1, 2, 3] | ID 1 | 0 | [] | false |
| 22:10:51 | `06f6c7bb22ef` | c / c | switch_completed | NOT_FOUND | [] | [] | 0 | [] | false |
| 22:11:13 | `d987281289b3` | b / b | switch_completed | FOUND | [1, 2, 3] | ID 1 | 0 | [] | false |

Physical counts read with SQLite `mode=ro`: b=7, c=2. The user heard the expected answers: blue lighthouse → not remembered → blue lighthouse. Cognitive Trace intentionally has no ASR/body text.

## New Trace field

`memory.diagnostics.recall_evidence_memory_ids` records only the selected Memory IDs used by the deterministic recall contract. It distinguishes answer evidence ID 1 from broad retrieval candidates 1/2/3 and never logs the answer text.

## Next order (user-directed)

1. Generalize Recall Intent for plan names, numbers, and equipment names with isolated tests.
2. Build a separate test venv; do not touch the runtime venv; run full pytest.
3. Wire TurnLatency to the real path, measure 30 normal turns, and trace duplicate speech from SpeechRequest through TTS and playback.
4. Connect Trace to normal Discord conversations.
