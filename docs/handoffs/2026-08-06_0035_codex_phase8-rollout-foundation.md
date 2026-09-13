# Phase 8 rollout resolver and atomic fallback foundation

## Scope and authority

- Local source of truth: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`.
- No Memory DB, persona, migration, persistent cognition setting, or historical Trace was changed.
- The Phase 7D four-turn Echo Guard hardware acceptance remains valid.

## Implemented

| Area | Result |
|---|---|
| Rollout | `CognitionRolloutResolver` resolves `disabled`, process-local `test_session`, process-local `production_session`, and explicit persistent `production`. Invalid/session values in config fail closed. |
| Frozen context | TurnMetrics, TurnFrame, and Trace retain requested/resolved rollout, activation source, config fingerprint, transport, and session epoch. |
| Controls | UI cycles OFF → TEST → PROD SESSION. Launcher accepts mutually-exclusive `--cognition-test-session` and `--cognition-production-session`. These do not persist config. |
| Fallback | A technical failure can fall back once only before tool/memory-write/SpeechRequest/playback effects. Intentional silence, gate rejection, explicit recall NOT_FOUND, stale epoch, and post-effect failures do not fall back. |
| Local/Discord | Both consume the resolver. Discord enabled normal turns invoke the existing no-LLM action selector and merge its safe fields into its normal Trace row. |

## Verification

| Command | Result |
|---|---|
| `.venv-test\Scripts\python.exe -m compileall -q neuro_voice` | passed |
| `.venv-test\Scripts\python.exe -m pytest -q` | `2937 passed, 1 skipped` in 99.47s |

The sole skip is `tests/test_tool_dialogue.py::test_a_symlinked_directory_cannot_escape` because this Windows account lacks symlink privilege. The privilege-independent path-escape unit coverage remains green.

## Required next manual acceptance (do not enable persistent production yet)

1. Start Local PROD SESSION and run the bounded eight normal/recall turns, then one existing safe read-only or test-scratch tool turn.
2. Run three Discord normal/repeated turns in the same temporary session.
3. Restart normally and verify `disabled` + `legacy`.
4. Confirm Trace has one row per turn, requested/resolved rollout and transport, no duplicate delivery, and no Memory writes/reflection.

## Rollback

- UI: cycle to OFF; the change applies at the next idle boundary.
- Launcher: omit both cognition-session switches.
- Persistent config is still `cognition.enabled=false` and `rollout_mode=disabled`; no config edit is required to return to Legacy.
