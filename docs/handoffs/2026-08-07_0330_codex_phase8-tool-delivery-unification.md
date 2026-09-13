# Phase 8 Tool Presenter delivery unification — Codex handoff

## Evidence and diagnosis

- Read-only inspection of `81d8be729c9b` found one accepted SpeechRequest and TTS job, but zero logical playback sessions/segments and no `trace_delivery_finalized`.
- This Trace does **not** prove either that audio played or that it failed physically. It proves the prior direct-reply route never registered physical delivery with TurnTracker.
- Cause: `_respond_direct_text()` called TTS and `SpeakerPlayback.play()` directly. Although it used the same device object, it bypassed `_speak_loop`'s TTS chunk, logical session, segment callback, seal, and finalizer ownership.

## Implemented delivery route

`Tool Result Presenter → SpeechRequest → Speech Gate → _respond_direct_text → _speak_loop → TurnTracker → SpeakerPlayback → logical playback session → physical segments → delivery finalization → ActionOutcome/Turn Closure`

- Direct deterministic replies now enqueue a one-text queue through `_speak_loop`; no direct Tool TTS/Playback path remains.
- `_await_delivery_finalization()` waits only for delivery terminal state. It does not synthesize, play, retry a Tool, replay audio, or invoke Legacy fallback.
- `completed` requires a logical session and completed segments. Missing session/segment is `failed`; incomplete segment is `interrupted`.
- Trace fields: `delivery_finalize_called`, `delivery_terminal_state`, `playback_started`, `playback_completed`, `trace_delivery_finalized`, `delivery_finalize_error`, plus existing delivery counts and IDs. No response text is added.

## Tests

- Focused delivery/Tool suite: `246 passed, 1 skipped`.
- Full isolated suite: `2968 passed, 1 skipped in 101.70s`.
- The sole skip is expected Windows symlink privilege.
- Coverage includes direct Tool-style reply through shared loop, no-delivery failure outcome, incomplete playback interruption, finalizer exception trace, streaming multi-segment one logical session, duplicate segment/session rejection, Tool one-attempt handling, and result presenter safety.

## Next hardware check (do not run until requested)

Fully restart and enter Local PROD SESSION. One existing safe Tool request is sufficient; do not add extra conversation. Accept only when the new Trace has one logical playback session, finalizer=true, completed terminal state, normal completed closure/outcome, and no replay/fallback/Memory write. Discord remains out of scope.
