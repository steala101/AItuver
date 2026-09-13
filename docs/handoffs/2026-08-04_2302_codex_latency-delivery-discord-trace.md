# 2026-08-04 23:02 JST — Codex — latency / delivery / Discord Trace

## Completed

- Recorded the accepted post-restart B→C→B recall smoke result in the preceding handoff. Valid turns were `12ee34199cbf`, `06f6c7bb22ef`, `d987281289b3`; do not reuse the older rejected IDs.
- Added `recall_evidence_memory_ids` to Cognitive Trace. It distinguishes retrieval candidates from the record used by the deterministic response contract.
- Generalized explicit deterministic recall, with isolated checks for `合言葉`, `計画名`, `番号`, and `機器名`. FOUND and NOT_FOUND still make zero recall LLM calls.
- Created only `.venv-test` (the runtime `.venv` was not changed), installed test-only dependencies, and ran the full suite.
- Connected Local `TurnMetrics` to `LatencyWindow`; Local events now include source-local rolling p50/p90/p95.
- Added final Trace fields:
  - `turn_latency`: safe per-turn `TurnMetrics` snapshot; use `total_ms` as `speech_end → play_start`.
  - `latency_summary`: source-separated rolling totals with sample count and p50/p90/p95.
  - `speech_delivery`: safe `speech_request`, `tts_job`, `playback` attempted/accepted/rejected counts plus `first_duplicate_stage`.
- Connected Discord normal-reply completion to the same Trace schema and project-root JSONL. Two background writers cannot interleave a JSONL line.

## Boundaries kept

- No Memory DB, backup, migration, deletion, restoration, or persona configuration was changed.
- `memory.write_enabled` remains false exactly as the controlled test setting.
- Runtime `.venv` was not repaired or modified.
- Trace excludes conversation/body/prompt text, raw audio, secret values, and delivery IDs.
- `duplicate_suppression_enabled` remains false: the new counts observe duplication without masking it.

## Verification

- `compileall neuro_voice`: pass.
- Targeted affected suites: `166 passed`.
- Final full separate test venv: `2892 passed, 2 failed` in 95.82s.
  - `tests/test_ktane_verify.py::test_the_prompt_forbids_inventing_for_unlisted_modules`: its Linux literal `/tmp/ktane_state.json` resolves to `C:\tmp` on Windows and could not create its temp fixture file.
  - `tests/test_tool_dialogue.py::test_a_symlinked_directory_cannot_escape`: cannot create a Windows symlink (`WinError 1314`).
  Both are host fixture/capability failures, not changed application assertions.

## Required next steps — do not reorder

1. Fully restart the app so the current source is loaded; do not change the test flags or Memory DBs.
2. Run one ordinary Local conversation first and confirm its Trace has `turn_latency`, `speech_delivery`, and `recall_evidence_memory_ids` where applicable.
3. Collect 30 ordinary Local turns. Report source `local_mic` only, `latency_summary` count, p50/p90/p95, and missing-total count. Do not infer percentiles from fewer valid `play_start` samples.
4. If duplicate speech is heard, locate that turn and compare `speech_delivery` in order: SpeechRequest, TTS job, Playback. With suppression still false, a rejected count identifies the first repeated delivery attempt without hiding the audio behavior.
5. Run one ordinary Discord conversation and confirm a single `selected=discord_conversation` Trace record with `source_type=discord_voice`; do not claim runtime acceptance from source/compile tests alone.
6. Re-run full pytest on a host with writable fixture path plus Windows symlink privilege, or Linux CI. Do not weaken the symlink escape test or alter production paths to satisfy the fixture.

## Do not do

- Do not proceed to Phase 8.
- Do not re-run persona/memory smoke conversations before the restart and trace-shape check.
- Do not enable duplicate suppression to conceal a recurrence.
- Do not manipulate SQLite across a mount or modify DBs while measuring latency/duplicates.
