# Phase 8 terminal intent regression repair

## Scope

- Local PROD SESSION only. Do not proceed to Tool or Discord acceptance yet.
- No Memory DB, persona, migration, persistent configuration, or historical Trace was changed.

## Read-only result that prompted this repair

- The three explicit-response probes in Local PROD SESSION completed with `ANSWER`, one SpeechRequest, one logical playback session, no replay, no Memory retrieval, and completed closure.
- The terminal-cue probe was spoken because its existing response plan had reason `activity_input_has_priority`; the old response-required bridge treated every responding plan as an ANSWER contract.

## Change

- `ResponsePlan.requires_response_contract` now limits the stronger cognitive ANSWER contract to existing `addressed` answer/continuation intents.
- Activity input and acknowledgement plans remain available to their owning dialogue/directive paths, but do not prohibit a valid end signal from selecting `REMAIN_SILENT` or `BRIEF_ACKNOWLEDGE`.
- No user-text classifier was added. The decision uses the existing plan role and reason.

## Verification

- Targeted: `161 passed` for silence, closure, and interaction tests.
- Full isolated test venv: `.venv-test\Scripts\python.exe -m pytest -q` -> `2945 passed, 1 skipped in 104.47s`.
- The sole skip is the existing Windows symlink privilege integration case; privilege-independent path-escape coverage remains green.

## Required next manual verification

1. Fully restart with persistent cognition disabled, then enable Local PROD SESSION while idle.
2. Say `もういいよ` once only.
3. Require exactly one of: `REMAIN_SILENT` with the complete `silence` block and zero SpeechRequest, or `BRIEF_ACKNOWLEDGE` with one logical playback session.
4. Require an Action Selector decision ID/source/reason, no Legacy fallback, no duplicate delivery, and no Tool execution.
5. The already-passing three explicit-response probes do not need repetition unless this terminal probe fails.

## Remaining gate

Do not continue to Tool or Discord until the terminal probe passes. Restart-to-Legacy acceptance also remains pending.
