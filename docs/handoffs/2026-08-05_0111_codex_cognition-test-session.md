# 2026-08-05 01:11 JST — Codex — cognition test-session control

## Completed

- Added a nonpersistent cognition test-session control: GUI `認知: ON/OFF` and launcher `--cognition-test-session`. Normal startup/restart remains Legacy.
- Added `neuro_voice/cognition/test_session.py`: `idle → test_session_starting → test_session_active`, plus queued stop. A request while a turn is active applies after that turn and increments the epoch.
- Local `TurnMetrics` and `TurnFrame` freeze effective enabled/mode/epoch. The final speech gate rejects a stale test-session epoch.
- Trace reports cognition enabled/mode/epoch, selector/gate/legacy-path booleans, selected action, and accepted SpeechRequest count.
- Moved normal SpeechRequest claim after cognitive silent selection, so `REMAIN_SILENT` produces zero claims.
- Explicit diagnostic search overrides “attention only” and favorites filters; `memory.write_enabled=false` remains visible and searchable even if either filter was left on.
- Search normalization accepts both `memory.write_enabled=false` and `memory.write_enabled = false`.
- Trace vocabulary is now split: `cognition.rollout_mode` is the frozen rollout (`disabled` or `test_session`) and `cognition.execution_path` is the actual route (`legacy` or `cognitive`). Old JSONL lines were not modified.

## Verification

- Targeted cognition/trace/turn tests: `80 passed`.
- Final full separate test venv after the Trace vocabulary split: `2903 passed, 1 skipped in 92.12s`.
- Sole skip: existing Windows symlink-privilege integration test.

## Explicit non-changes

- No `config.yaml` cognition persistence.
- No Memory DB/persona/migration/backup changes and no real conversation.

## Next ordered acceptance

1. Normal restart and requested 5-turn Legacy smoke: cognition false, Memory `NOT_APPLICABLE`, retrieved 0.
2. UI `認知: ON`, then requested 3-turn cognition smoke: enabled true, `test_session`, selector/gate true, legacy false, one SpeechRequest each.
3. After both pass, perform the one 30-turn cognitive measurement; do not enter Phase 8 before it.
