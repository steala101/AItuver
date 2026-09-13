# Phase 8 Local PROD Tool Result Presenter — Codex handoff

## Scope

- Local `web_search.search` only. No user-operated search was performed during this change.
- No Memory DB, persona, migration, persistent config, or historical Trace was changed.
- Discord is explicitly out of scope.

## Result trust boundary

`neuro_voice/cognition/search_result_presenter.py` owns `SearchResultView` and deterministic presentation.

- Allowed fields: `result_id`, cleaned `title`, canonical `url`, `normalized_domain`, `source_type`, rank, relevance, official-candidate bit.
- It accepts `http`/`https` only and rejects malformed URLs, credentials in URLs, control characters, and HTML-like URLs. URL query/fragment are removed.
- HTML and snippets are never retained in the Tool result view. Provider data is treated as untrusted again when presented.
- Instruction-like titles become their domain for display/TTS. They cannot become instructions, prompts, actions, or Tool calls.

## Local execution and response route

1. Tool handler retrieves raw provider rows and immediately creates compact views.
2. `ToolRuntime` sends the existing proposal/gate/executor path through a one-attempt override for this PROD-session `web_search.search` call.
3. `SearchResultPresenter` re-normalizes the compact views. It selects the first official candidate (currently only `openai.com` and subdomains) or rank one.
4. Successful result: UI receives one cleaned title/URL event and TTS receives a one-sentence grounded report. Zero results: deterministic not-found report. Tool error: deterministic failure report.
5. Neither Tool error nor presenter handling invokes Legacy fallback or a second Tool execution. A presenter exception becomes one deterministic display-error response.

## Trace evidence (content-free)

`tool` contains `tool_result_status`, `normalized_result_count`, selected result IDs/domains, `tool_result_used_by_planner`, `result_presenter_type`, `final_response_grounded`, and latency `tool_result_to_response_ms`. It contains no result title, URL, snippet, query, HTML, or raw Tool output.

## Automated verification

- Focused: `182 passed, 1 skipped`.
- Full isolated test suite: `2964 passed, 1 skipped in 85.16s`.
- The sole skip is the expected Windows symlink-privilege test.
- Covered: normal one-result selection, zero results, malicious title/URL filtering, Tool error, one-attempt failure, response-required Tool selection, duplicate execution rejection, and content-free trace evidence.

## Before hardware confirmation

Do not initiate the request from Codex. On a fresh Local PROD SESSION, user should later speak exactly once:

`OpenAIの公式サイトを検索して`

Expected UI: one cleaned title plus URL. Expected TTS: a short grounded result mentioning the selected official candidate; never a generic execution-only acknowledgement. Confirm proposal/execution count one, `final_response_grounded=true`, one SpeechRequest/logical playback, no replay, no fallback, no Memory writes, completed closure/outcome. Do not proceed to Discord until this evidence is reviewed.
