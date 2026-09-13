# Phase 8 Local PROD silence contract repair

## Authority and scope

- Local source of truth: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`.
- The user stopped additional speech. This work inspected Trace read-only and changed only cognition/Trace/test/documentation code.
- No Memory DB, persona, migration, persistent config, or historical Trace was changed.
- Do not proceed to Tool or Discord acceptance until the bounded Local regression below passes.

## Read-only finding

The eight newest current-process PROD-session Trace rows were 01:15:23–01:17:42 JST. The silent row was:

- Turn: `6ecff02d0db6`, 01:15:52 JST.
- Selector result: `REMAIN_SILENT`; decision ID `76524a8575a9`; decision reason `user_is_ending,nothing_to_close`.
- Execution: planner route `silent`; no SpeechRequest, TTS, or Playback; `silent_completed`.
- It was not an exception, an empty generated response, an Echo Guard result, or a post-delivery suppression.
- Retrieval trigger: `similar_to_past_failure`, IDs `203/205/204`; its only recorded action adjustment was `continue_previous_topic:+0.108`. It did not add a silence adjustment.

The apparent missing `selected_action` was a query-path mistake: it is Trace top-level `selected`, not `cognition.selected_action`. The actual observability gap was the absence of explicit silence provenance and completed response-delivery/outcome fields for the non-speech branch.

## Implemented

- `CognitiveTrace` now records a privacy-safe `silence` block with `action_decision_id`, `decision_source`, `reason_code`, `suppression_reason`, and frozen response-required provenance. No user text or response text is added.
- `VoicePipeline` sends the existing `ConversationKernel` response plan into `Mind.cognitive_state`; no new text-matching classifier was added.
- `CognitiveKernel` treats a frozen response-required plan as an input contract and selects ANSWER where available, so Memory/closure scoring cannot pick `REMAIN_SILENT` for a required response.
- `ActionExecutionGate.silence_contract()` validates a silent decision. The Local non-speech path now uses the same authoritative `_cognitive_outcome()` closure as a spoken turn.
- Missing silent provenance cannot end as `silent_completed`. Before effects it may use the existing once-only Legacy fallback; otherwise it produces an explicit failed outcome.

## Verification

- Targeted: `47 passed`.
- Full test venv: `.venv-test\Scripts\python.exe -m pytest -q` → `2944 passed, 1 skipped in 93.81s`.
- The one skip is `tests/test_tool_dialogue.py::test_a_symlinked_directory_cannot_escape`, justified by missing Windows symlink privilege. Privilege-independent escape coverage remains green.

## Required next manual verification

1. Fully restart; keep persistent config at `cognition.enabled=false` / `rollout_mode=disabled`.
2. Enable Local PROD SESSION only while idle.
3. Say the known explicit-response regression utterance twice and one semantic paraphrase once. Each must show `selected=answer`, `SpeechRequest=1`, one logical playback session, completed closure, no replay, no Memory retrieval unless independently justified, and no Tool execution.
4. The originally silent turn was a terminal cue. Exercise it once only as a terminal-cue probe: it may select `REMAIN_SILENT` or a brief acknowledgement, but must record the full `silence` block; it must not use fallback.
5. Do not rerun the eight-turn suite unless these four regressions fail. Do not proceed to Tool/Discord until they pass.

## Remaining risk

`R-047` remains open: safe-tool, Discord, and restart-to-Legacy Phase 8 acceptance are not yet performed.
