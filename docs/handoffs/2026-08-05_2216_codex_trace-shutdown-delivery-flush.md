# 2026-08-05 22:16 JST — Codex — trace shutdown delivery flush

## Status

Completed for the one-turn streaming playback acceptance.

## 22:12 hardware evidence

- Current local source was started at 22:11:59 and handled `短く自己紹介して` at 22:12:20.
- The response synthesized four chunks (about 17.9 seconds of audio total) and the application began shutdown at 22:12:41, before final physical playback closure.
- `logs/cognitive_trace.jsonl` therefore remained at 21:48:12. This was not a missing configuration: the 22:11:59 startup diagnostic confirms Trace was enabled, initialized, and resolved to the current project root.
- No historical Trace was constructed or appended. This run remains unaccepted because the required delivery evidence was not persisted.

## Fix

- `VoicePipeline` retains asynchronous playback-closure Trace tasks.
- `shutdown()` cancels and awaits those tasks before closing the `TraceWriter`.
- A cancelled closure task emits exactly one final Trace snapshot, marking `speech_delivery.trace_delivery_finalized=false` when actual playback did not complete. The normal writer close then flushes for its bounded interval.
- This is shutdown-only diagnostic work: it does not block first audio, add disk I/O to the response path, or change Memory/persona/config state.

## Tests

- New isolated check cancels a pending playback closure and confirms one partial Trace is emitted.
- Targeted delivery/outcome suites: `42 passed`.
- Full separate `.venv-test`: `2916 passed, 1 skipped in 96.80s`.
- Only skip: `tests/test_tool_dialogue.py:580`, unavailable Windows symlink privilege.

## Next hardware check

1. Fully restart using the current local source.
2. Enable the existing cognition test session as before.
3. Say only `短く自己紹介して`.
4. Let all speech finish; do not close the application until after the Trace line is written.
5. Accept only when the fresh line shows:
   - one logical playback session;
   - one or more physical segments, all unique;
   - zero replayed segments;
   - `duplicate_stage=none`;
   - `trace_delivery_finalized=true`.

## Hardware acceptance (22:17 JST)

- Fresh turn: `c966f56ef6c7`.
- Cognition contract: enabled, `test_session`, `cognitive`.
- Delivery: one SpeechRequest; one logical playback session; four physical segments; all four unique; no replayed segment; no time overlap; no duplicate stage.
- The four segments completed sequentially and the Trace was finalized normally (`trace_delivery_finalized=true`).
- This accepts the streaming-delivery instrumentation and rules out Playback-layer duplication for this turn. It does not change the historical `b6e9958e5590` verdict, which remains unclassifiable from its old Trace.

## Next work

Before the final 30-turn measurement, inspect why normal turns still enter retrieval through `unresolved_obligation`; do not begin the measurement until that trigger policy is reviewed.
