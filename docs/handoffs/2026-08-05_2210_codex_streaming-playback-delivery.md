# 2026-08-05 22:10 JST — Codex — streaming playback delivery accounting

## Status

Partial. Implementation and automated checks are complete. Do not run the 30-turn measurement yet; one post-restart, one-turn hardware check is required first.

## Historical diagnosis

- Target `b6e9958e5590` had one SpeechRequest, five TTS chunks, and two old `Playback` accepts.
- The old counter was a physical-chunk counter, not a logical-session counter. It therefore could represent normal streaming rather than duplicate audio.
- The historical JSONL has no delivery IDs, fingerprints, segment index, or timing. The requested distinction for that old turn cannot be reconstructed without inventing evidence. Its verdict is **undetermined**, not duplicate.

## Changed delivery contract

`speech_delivery` now contains these privacy-safe fields:

- `speech_request_count`
- `tts_chunk_count`, with `tts_jobs` containing ID, chunk index, text hash, and audio hash
- `logical_playback_session_count`, with session IDs and start/end timestamps
- `playback_segment_count`, `unique_playback_segment_count`, `replayed_segment_count`
- `playback_segments`, containing segment ID, session ID, chunk index, TTS job ID, hashes, start/end timestamps, and completion state
- `overlap_detected` and the first duplicate stage

No conversation text, prompt, raw audio, or PCM is written. Hashes are truncated SHA-256 fingerprints for correlation only.

## Runtime behavior

- `PlayedTextTracker` owns one `playback_session_id` per response.
- Local and Discord both register each chunk before delivery, mark actual playback start from their device/output callback, and record completion.
- Multiple unique segments in one session are normal streaming.
- Different session for the same SpeechRequest is rejected as `playback_session_duplicate`.
- Same segment ID, or same session plus segment index plus audio hash under a different ID, is rejected as `playback_segment_duplicate`, even with broad duplicate suppression still configured off.
- The normal Trace line is finalized asynchronously after its playback sessions close, with a 30-second bound. This does not delay first audio and does not add synchronous disk I/O or `fsync`.

## Verification

- Added tracker coverage for one SpeechRequest with two unique streaming segments, repeated segment ID, repeated index/hash under a new ID, and a second logical session.
- Updated Local/Discord source-wiring checks and the Discord playback timing test to inspect its method boundary rather than a brittle fixed character range.
- Targeted: `41 passed in 1.03s`.
- Full separate `.venv-test`: `2915 passed, 1 skipped in 79.36s`.
- Only skip: `tests/test_tool_dialogue.py:580` because Windows symlink privilege is unavailable.

## Explicit non-changes

- No Memory DB read/write, restore, deletion, migration, or persona/config mutation.
- No historical Trace rewrite or backfill.
- No Phase 8 work.

## Next hardware check (only one turn)

1. Fully terminate the old process, then start the current local source once.
2. Keep cognition test session enabled as already instructed.
3. Say only: `短く自己紹介して`.
4. Inspect its fresh Trace after playback finishes. Pass only when:
   - `logical_playback_session_count=1`
   - `playback_segment_count>=1` (multiple segments allowed)
   - `unique_playback_segment_count=playback_segment_count`
   - `replayed_segment_count=0`
   - `duplicate_stage=none`
   - no time overlap for different segments.
5. Only after this pass, inspect the unrelated unresolved-obligation retrieval reason, then proceed toward the final 30-turn measurement.
