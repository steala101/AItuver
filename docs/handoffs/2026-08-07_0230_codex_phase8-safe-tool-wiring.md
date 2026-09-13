# Phase 8 Local PROD safe Tool wiring — Codex handoff

## Scope and authority

- Local folder is authoritative: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`.
- This change is limited to the existing `web_search.search` path. No Tool was added.
- Do not touch Memory DBs, persona settings, migrations, persistent tool flags, historical Trace, Tool scope, Discord, or Phase 9 work.

## Why the prior request did not search

The prior user utterance `OpenAIの公式サイトを検索して` was processed by the pre-wiring Legacy route (`turn_id=704f55e5a3ee`): cognition was disabled, no ToolRuntime proposal/execution was created, and the normal speech route replied. This was a missing call-site/rollout wire, not a search-provider or Memory failure.

## Implemented route

1. In Local `production_session`, an explicit read-only search intent adds the existing `EXECUTE_TOOL` candidate for `web_search.search`; the normal response-required filter preserves this bounded candidate rather than dropping it for `ANSWER`.
2. `ToolRuntime.set_read_only_session(True)` is a process-local override. It enables only `web_search`; persistent config remains unchanged and write effects remain rejected.
3. The existing Tool proposal, permission, Tool Gate, executor, idempotency ledger, and verification path run once.
4. Once Tool execution starts, Legacy fallback cannot run it again. The Tool result text never goes to a prompt or TTS; only verified status chooses a fixed, short success/failure report.
5. Trace records safe metadata only: proposal ID, permission, execution ID/status/counts, result availability/use, no side effect committed, and normal delivery/closure fields. It records neither query arguments nor Tool result text.

## Tests

- Focused: `.venv-test\Scripts\python.exe -m pytest -q tests\test_tool_dialogue.py tests\test_tool_execution.py tests\test_phase8_silence_contract.py`
  - `175 passed, 1 skipped` (Windows symlink privilege only).
- Full: `.venv-test\Scripts\python.exe -m pytest -q`
  - `2957 passed, 1 skipped in 85.44s`.
- Compile check passed for the changed runtime/pipeline/trace modules.

## Required user-operated hardware probe

Codex must not initiate it. First fully exit the currently running app so it loads the new local code. Start normally, wait until IDLE, then select **PROD SESSION** in the UI and say exactly once:

`OpenAIの公式サイトを検索して`

Expected successful result: one short final spoken report after one READ_ONLY `web_search.search` execution. If the external search is unavailable, one fixed failure report is expected; it must not retry through Legacy.

Inspect only the new turn's Trace:

- `selected_action=EXECUTE_TOOL` before the outcome route;
- `tool.tool_id=web_search`, `tool.tool_proposal_id` nonempty, `tool.permission_level` allowed, one accepted execution, started/completed true;
- `tool.side_effect_committed=false`, `tool.fallback_attempted=false`, `tool.tool_result_used_by_planner=true` on verified success;
- one final SpeechRequest and one logical playback session with no replay;
- `memory.write_decisions=[]`, normal Turn Closure/ActionOutcome.

Stop after that one turn. Do not progress to Discord until the user-operated probe is read-only verified.

## Residual risks

- The user-operated hardware request is still pending, so this is not a Tool rollout approval.
- External network/provider availability can cause the fixed failure report; do not add a fallback Tool execution.
- Discord intentionally remains out of this route.
