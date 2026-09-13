# 2026-08-05 JST — Codex — retrieval trigger, LLM diagnostics, Windows pytest

## Completed

- Located the real Local context caller: `VoicePipeline._inject_mind()` delegates immediately to `ContextAssembler`; the latter had used `include_recall=recall_budget > 0`. With the configured 900ms budget, that caused the prior 30 ordinary turns to retrieve Memory every time.
- `ContextAssembler` now obtains `retrieval_trigger(user_text)` without an LLM. It passes `include_recall=true` only for an explicit past/labelled-recall reason; normal turns pass false and `not_applicable`.
- Removed the broad proper-noun fallback from the trigger. Explicit past-reference and labelled recall (passphrase, plan name, number, equipment name) remain available.
- `Mind.build_context()` records a complete no-search Trace state: `memory_trigger_result=NOT_APPLICABLE`, `memory_trigger_reason=not_applicable`, zero retrieval counters, `recall_status=NOT_APPLICABLE`, zero recall LLM calls, and no evidence IDs.
- Added safe `llm_diagnostics` Trace fields. They contain no prompt/transcript: prompt-build ms, estimated token count, prefix reuse, retrieved count, initial provider token, known zero/no queue contention, and native Ollama load/prompt-eval durations only if the OpenAI-compatible stream actually exposes them.
- Fixed full pytest Windows fixtures. The KTaNE test uses `tmp_path`; symlink integration skips only for `WinError 1314`, while an independent mocked resolved-path unit test remains active.

## Verification

- Affected retrieval/trace/latency/cognition/Ollama suites: `101 passed`.
- Windows fixture suites: `129 passed, 1 skipped`.
- Full independent `.venv-test`: `2897 passed, 1 skipped` in 91.61s.
- No Memory DB, backup, persona setting, migration, runtime `.venv`, or write-gate was changed.

## Important evidence / next order

1. Fully restart before any live measurement: current running process has the old ContextAssembler code.
2. Existing Trace and conversation metrics intentionally omit ASR/user text, and DB may not be read for this task. Therefore the exact prior 30 ASR strings cannot be replayed from local artifacts. The automated fixed 30-item ordinary corpus proves trigger=0 and the actual `ContextAssembler → Mind.build_context` call proves no search is invoked; a true exact-ASR replay needs the user-provided transient corpus.
3. After restart, run the one requested 30-turn ordinary session. Expect `memory.diagnostics.trigger_result=NOT_APPLICABLE` and `retrieval_trigger_reason=not_applicable` for the ordinary corpus, with `retrieved_memory_count=0` in `llm_diagnostics`.
4. Explicit B→C→B recall must still be checked as a separate controlled test if rerun: trigger true; deterministic response contract remains FOUND → NOT_FOUND → FOUND, zero recall LLM calls, and writes remain off.
5. The stage-controlled cognitive path cannot yet be accepted in a live session: `VoicePipeline.start_cognition_test_session()` exists, but `rg` found no UI or launcher caller. Do not set `cognition.enabled=true` / `rollout_mode=test_session` and claim it active until an explicit caller/control exists or a user-approved temporary invocation is defined.
6. Do not proceed to Phase 8. A real cognition-enabled latency comparison and a real Discord normal conversation Trace remain acceptance work.

## Changed files

- `neuro_voice/realtime/context_assembler.py`
- `neuro_voice/cognition/recall.py`
- `neuro_voice/mind/mind.py`
- `neuro_voice/cognition/trace.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/llm/openai_compat.py`
- `tests/test_memory_retrieval_wiring.py`
- `tests/test_ktane_verify.py`
- `tests/test_tool_dialogue.py`
- `docs/SHARED_CHANGELOG.md`, `docs/CURRENT_ARCHITECTURE.md`, `docs/CODEMAP.md`, `docs/KNOWN_RISKS_AND_DEBT.md`
