# 2026-08-05 21:32 JST — Codex — cognitive outcome single payload

## Status

Partial: code and automated verification are complete. Do not treat the cognition warmup as accepted until the one-turn hardware check below succeeds.

## Cause and correction

- The 21:21 cognitive warmup failed before Trace enqueue with `TypeError: VoicePipeline._emit() got multiple values for keyword argument 'obligations'`.
- Cause: `_cognitive_outcome()` used both `**outcome.snapshot()` and a separate `obligations=...`; `ActionOutcome.snapshot()` already contains `obligations`.
- Owner is now `ActionOutcome`. After `CognitiveKernel.apply_outcome()` settles the obligation IDs, they are written to `ActionOutcome.affected_obligation_ids` once.
- `_emit_cognitive_outcome()` constructs one `emit_payload = outcome.snapshot()` and sends exactly that payload to `_emit`. The same object is used as `trace.outcome_summary`.
- `ActionOutcome.turn_id` is now populated from the frozen decision/metrics turn ID.
- REMAIN_SILENT follows the same helper. Its Trace records zero accepted SpeechRequests and the outcome event is emitted once.

## Error boundary

- `_emit_trace_safely()` catches and logs only Trace writer enqueue errors. A failing Trace writer therefore cannot fail a cognitive turn.
- ActionOutcome application, world-state update, internal outcome observation, and Speech Gate failures are intentionally not caught as legacy fallback. The response `finally` preserves active-response/session cleanup before such an error propagates.
- The failed 21:21 turns remain error-log evidence only. No historical JSONL line was reconstructed or appended.

## Limited audit

The requested duplicate-key audit was limited to `obligations`, `selected_action`, `speech_request_count`, `persona_epoch`, and `turn_id` in the cognitive outcome, emit, silent completion, Trace, and turn closure paths. No remaining `snapshot()` expansion combined with a second same-named keyword was found in this scope.

## Verification

- New `tests/test_cognitive_outcome_payload.py`:
  - ANSWER with empty obligations: one event, one Trace, one accepted SpeechRequest.
  - ANSWER with obligations: one source/one state observation and one payload value.
  - REMAIN_SILENT: one outcome event, one Trace, zero SpeechRequests.
  - Same outcome re-delivery: one event and one Trace because the turn decision is consumed.
  - Trace writer failure: cognitive state completion continues.
  - ActionOutcome and Speech Gate failures: propagate rather than being hidden.
- Existing coverage continues to verify same-turn SpeechRequest commit deduplication and stale cognition-epoch final-gate rejection.
- Targeted suite: `122 passed`.
- Full separate test venv: `2910 passed, 1 skipped in 92.57s`.
- The only skip is `tests/test_tool_dialogue.py:580`, Windows symlink privilege unavailable.

## Explicit non-changes

- No Memory DB reads or writes; no restore, deletion, migration, persona mutation, or backup operation.
- No `config.yaml` changes and no persistent cognition override.
- No normal Trace added for the failed 21:21 warmup turns.

## Next acceptance — stop after the first turn

1. Fully close the old application process and start the current local code once.
2. In the UI, enable the cognition test session while idle.
3. Speak exactly: `2足す2は？`
4. Verify its one fresh Trace has:
   - `cognition.enabled=true`
   - `cognition.rollout_mode=test_session`
   - `cognition.execution_path=cognitive`
   - `action_selector_used=true`
   - `speech_gate_active=true`
   - `legacy_response_path_used=false`
   - SpeechRequest `1`, Playback `1`, Trace line `1`, no exception.
5. Only then continue with the remaining two warmup utterances. The 30-turn measurement remains blocked until all three warmup turns pass.
