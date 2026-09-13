# Phase 7D: Explicit ANSWER echo recovery

## Scope

- Local authority: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`.
- Phase 8 was not started.
- No Memory DB, persona, config, migration, restoration, deletion, or historical Trace was changed.
- The app should remain IDLE with cognition OFF until the requested regression run.

## Confirmed failure from the 30-turn trial

The two user-reported no-audio turns were:

| Turn | Trace finding |
|---|---|
| `83477cb67427` | addressed `ANSWER`; Speech Gate allowed; one SpeechRequest; zero TTS chunks, logical sessions, and Playback segments. |
| `d6da9eb2e328` | Same path and counters. |

Both LLM attempts hit `ReplyEchoGuard` historical assistant-response similarity (`0.88` and `1.00`). The old path cleared the primary draft, cleared a historically similar retry, and deliberately closed silently. This is not the `turn_integrity.duplicate_suppression_enabled` delivery flag and is not a TTS/Playback failure.

The captured interval has 31 Trace turns for the user-declared 30 inputs. Cognitive Trace intentionally omits ASR text, so no truthful one-to-one prompt ordinal can be reconstructed from it. Do not fabricate A/B/C block statistics from that interval. A future measurement marker/epoch is still needed; it is outside this fix.

## Implemented minimal fix

1. `ReplyEchoGuard` remains a detector of **historical** textual similarity only.
2. Same-turn hard duplicates remain exclusively owned by the SpeechRequest/TTS/Playback delivery ledger; no historical text match is used as an idempotency key.
3. On historical similarity, Local and Discord may make one regeneration attempt. Resolution order is:
   - valid non-similar regeneration;
   - undelivered primary (including when regeneration is empty or historically similar);
   - a deterministic safe fallback only when neither text exists.
4. Speech Gate denial and `REMAIN_SILENT` still create no fallback/SpeechRequest.
5. For a speech-permitted cognitive `ANSWER`, final status is now `failed` with `response_empty` or `tts_or_playback` when no logical playback session exists. It is `completed` only with a logical session; the corresponding ActionOutcome has speech generation and turn closure set true only then.
6. Trace now emits privacy-safe `response_delivery` fields only:
   - `echo_guard_checked`, `echo_match_scope`, primary/regenerated similarity scores;
   - regeneration attempted, echo resolution, final-response-empty;
   - speech-generation-completed, turn-closure-completed, action-outcome-status, delivery-failure-stage.

No response text, prompt, audio, or hash of such text is stored in these fields.

## Tests

Added coverage for historical primary retention, empty regeneration, valid regeneration, and true-empty fallback need. Added cognitive Outcome coverage for missing logical delivery (must fail) and accepted logical delivery (may complete). Existing delivery tests keep hard same-turn duplicate rejection separate.

```text
targeted: 69 passed
full:     2931 passed, 1 skipped in 84.62s
```

The sole skip is `tests/test_tool_dialogue.py:580` because Windows symlink privilege is unavailable.

## Required hardware regression

Fully restart the local app, enable the UI cognition test session, then speak each item twice, waiting for audio completion between turns:

1. `月曜日の次は？`
2. `今日は静かに過ごしたい。短く返して`

For all four fresh Trace turns require:

```text
selected_action=ANSWER
speech_request_count=1
logical_playback_session_count=1
replayed_segment_count=0
final_response_empty=false
speech_generation_completed=true
turn_closure_completed=true
action_outcome_status=completed
delivery_failure_stage=""
persona_leak=false
Memory=NOT_APPLICABLE / retrieved=0
Tool execution=0
```

Do not rerun the 30-turn measurement until these four targeted regressions pass. If they pass, treat the prior 29 normal deliveries as retained evidence and proceed according to the user’s next instruction.
