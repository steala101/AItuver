# Grounded search and Discord DAVE reliability — Codex handoff

## Scope

- Authoritative local folder: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`.
- Implemented only the requested search grounding, Discord latest-input handling, DAVE music ducking, and DAVE leave confirmation.
- Did not change any Memory DB, persona, migration, persistent configuration, historical Trace, or run a real Discord session.

## Diagnosis

1. `SearchResultPresenter` was intentionally a Phase 8 safety probe: title/URL only, with the fixed text `「○○を見つけたよ」`. It could not answer a factual request substantially.
2. Discord had a separate older path: `DeepSearch` formatted raw page text into an LLM system message. Nothing verified that the LLM's story/summary was supported, so a rakugo title could become invented narration.
3. In DAVE direct mode, `_direct_music_pump()` stopped sending music whenever assistant audio was active, because TTS and music were two competing PCM writers.
4. Each Discord final input became an independent task. No ingress generation invalidated an older STT/LLM task before it could answer.
5. Sidecar `leave` emitted `left` but Python did not wait for it; the UI could declare success after a failed/unready DAVE voice session.

## Implemented paths

### Search

`DeepSearch evidence rows → SearchResultView(title/url/domain/sanitised excerpt) → deterministic extractive presenter → UI/TTS`

- Only a bounded safe excerpt can be spoken. HTML, control characters, credentialed URLs, unsafe schemes, and instruction-like evidence are rejected.
- No web title, snippet, HTML, or page text becomes an instruction. The presenter does not call an LLM.
- If a result has no usable excerpt, it explicitly says that the content could not be confirmed. It does not complete the story from the title.
- Local Tool uses `DeepSearch.evidence_results`; Discord explicit search uses `DeepSearch.search_many_results` plus the same presenter. Discord does not inherit the Local process-only Tool enablement.
- Trace stores only existing result IDs/domains/count/grounded bit, never the evidence text.

### Discord input, music, leave

- `_enqueue_utterance()` now emits a monotonic input epoch and requests cancellation of the prior active response. `_respond_worker`, `_handle_utterance`, and group wait reject an old epoch before response effects.
- DAVE music remains one PCM stream. When music is active, TTS enters `MixedAudioSource`; music is mixed at 70% while speech is queued. No second `play_pcm` writer is used.
- Node sidecar awaits its leave call before sending `left`. `DirectDAVEReceiver.leave()` waits up to 8 seconds for `left` or a command error. `DiscordBridge.leave()` returns failure and preserves the connected state on failed confirmation.

## Verification

- Syntax/import: `py_compile` for changed Python modules; `node --check discord_receiver/receiver.js`.
- Focused: `247 passed, 1 skipped` (sole skip is Windows symlink privilege).
- Full `.venv-test`: `2974 passed, 1 skipped in 112.15s` (same sole skip).
- New coverage: extractive evidence / malicious evidence omission; DAVE leave success and command-error propagation; monotonically increasing Discord input epoch; DAVE music path uses the mixed writer and does not pause for TTS.

## Required user-operated acceptance (do not auto-run)

After a complete restart, use Discord with no concurrent Local session:

1. Ask for a short factual explanation of `饅頭こわい` after explicitly requesting search. Expected: a short source-backed excerpt/summary, or an honest inability to confirm; no invented plot continuation. The UI result card should show a title/domain/URL and bounded excerpt.
2. Start music, then ask one short question. Expected: music continues at lower volume during TTS and returns to normal after it. Do not use overlapping speech for this first test.
3. While a longer answer is still thinking or speaking, say a different short direct question. Expected: only the latest question receives a final answer; no old answer resumes afterward.
4. Use the UI leave control. Expected: it leaves the Discord VC. If the sidecar is unready/disconnected, the UI must show a failure rather than disconnected success.

Inspect only metadata/event names and Trace fields; do not copy raw audio, transcript, web excerpts, or secrets into reports.

## Remaining risks

- This is a bounded extractive presenter, not a general research synthesizer. It intentionally cannot provide a complete retelling of copyrighted works or bridge facts absent from fetched evidence.
- DAVE physical mix/leave behavior requires the above hardware acceptance; automated tests cannot establish Discord gateway state or perceived volume.
- Local and Discord still retain distinct high-level response orchestrators (existing R-003). This change aligns the requested boundaries but is not the full common Response Runtime refactor.
