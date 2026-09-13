# Handoff: Activity開始意図・自己経験grounding・Conversation Move契約

- Date: 2026-07-30 00:11 JST
- Agent: Codex
- Status: completed
- User request: 合意済みの優先1〜3（Activity開始誤判定、根拠ある自己経験、Conversation Move契約）を実装。

## Constitution alignment

- 第1条1.1: 発話を独立commandとして扱わず、文脈と意図から応答を選ぶ。
- 第1条1.2/1.4: 曖昧な記憶・架空経験を人格表現として作らない。
- 第2条: 質問末尾、response_id、取得根拠のような決定可能事項はコードで検証する。
- 第3条: メインLLMは一つ。追加判定LLM・別会話エンジンは作らない。

## Before / root causes

1. `intent_plan._CONTINUING`と`_SHAPES`が`TRPG`、`ラジオ`、`実況`等の名称だけでも候補化し、履歴・意見・情報質問をActivity開始へ反転できた。
2. `Mind`はtranscript/memoryを取得していたが、「今回の過去会話質問に根拠があるか」をPlanner/最終出力へ構造化して渡していなかった。
3. `ConversationPlan.ask_follow_up`等はprompt上の方針で、Local/DiscordのTTS・履歴確定前に検証されていなかった。
4. Local/Discordで話者未確定時、`build_context()`が既定local keyを使っていたためDiscord planと検証keyがずれる余地があった。

## Implemented

### 1. Activity start intent

- `is_activity_start_request()`を追加。
- Activity名に加えてassistantへ向いた開始行為を要求。
- 「前にTRPGやったの覚えてる？」「TRPGって何？」「ラジオはどう思う？」「実況の話をしてた」を開始対象外に固定。
- 既存の「TRPGやろう」「GMやって」「なりきりで話そう」「ラジオトークして」「実況して」は維持。
- `continuation_candidate()`と`plan_from_request()`の両入口に同じguardを適用。

### 2. Grounded self-experience / recall

- 明示的な会話履歴質問を`recall_requested()`で分類。
- `Mind.build_context()`が実際のranked memory、transcript、reference transcriptの有無を`recall_grounded`としてPlannerへ渡す。
- 根拠ありは`HONEST_RECALL`、なしは`SAY_NOT_REMEMBERED`。
- 高確信な一人称の読書・視聴・プレイ・訪問等は、provenanceがない限り「実際に体験した記録はない」へ置換。
- 現在知覚、明示的否定、fiction/hypotheticalは除外。

### 3. Conversation Move contract

- 既存`ConversationPlan`へ`conversation_move`、`response_id`、`source`、recall状態を追加。
- Move: `DIRECT_ANSWER / BRIEF_REACTION / SHARE_OPINION / PLAYFUL_REACTION / EXTEND_TOPIC / ASK_QUESTION / REPAIR / SUPPORT / HONEST_RECALL / SAY_NOT_REMEMBERED`。
- 質問は`ASK_QUESTION` Moveの時だけ許可。通常のユーザー質問は`DIRECT_ANSWER`を優先。
- `ConversationContract`を`DialogueIntelligence`が所有し、同じresponse_id/surfaceだけへ適用。
- Local/DiscordともTTS直前とfinal reply確定時に検証。
- TTSチャンク境界をまたぐ一人称経験主張は短い保留バッファで一文に戻してから検証し、生成終端では句点なし断片もflushする。
- Localの生成終端でcancel・空文・TTS失敗が起きても、終端シグナル消費後に待機し続けない。
- UI streamingはprovisional、`assistant_done.text`をcanonical finalとして表示。
- Activityのstate-changing出力は既存Activity validatorを優先し、Conversation Contractを適用しない。

## Files

- `neuro_voice/dialogue/intent_plan.py`
- `neuro_voice/dialogue/conversation_contract.py`
- `neuro_voice/dialogue/conversation_planner.py`
- `neuro_voice/dialogue/surface_realizer.py`
- `neuro_voice/dialogue/intelligence.py`
- `neuro_voice/dialogue/__init__.py`
- `neuro_voice/mind/mind.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `neuro_voice/ui/assets/index.html`
- `tests/test_conversation_contract.py`
- `tests/test_behavior_directive.py`
- `tests/test_plan_from_request.py`
- `tests/test_conversation_generation.py`
- shared docs / ADR-0002 / D-028 / R-039

## Verification

- Syntax: modified Python files `py_compile` success.
- Focused first run: 3 failures detected and fixed:
  - explicit non-experience was overmatched,
  - `ラジオトークして` was not recognized,
  - past `実況の話をしてた` was recognized.
- Related regression after final fixes: `122 passed`.
- Conversation Contract suite: `11 passed`.
- Full suite: `1111 passed / 2 skipped`.
- Split TTS chunk / final-fragment flush regression included in the 11 contract tests.
- Skip reason: `pyflakes` unavailable for `test_no_undefined_names.py`; direct `py_compile` succeeded.

## Performance / privacy

- Added LLM calls: 0.
- Added DB migration/store: 0.
- Runtime cost: bounded regex and short sentence splitting.
- Logs contain contract reason codes only; conversation body is not copied.

## Remaining acceptance

Restart the GUI and test:

1. 「前にTRPGやったの覚えてる？」でDirectiveが作られない。
2. 「TRPGやろう」でNARRATIONが作られる。
3. 普通の質問への返答末尾に毎回質問が付かない。
4. 実際に記録がある過去会話質問は具体的、ない質問は率直に思い出せないと言う。
5. 「その原作読んだ？」等で未根拠な読書経験を作らない。
6. LocalとDiscordで同じ結果になる。

Observe `Conversation contract adjusted ...` reason codes. Body text must not be added to logs.

## Rollback

If the contract is too conservative:

1. First narrow only the offending regex in `conversation_contract.py` and add the exact regression case.
2. Do not remove response_id/source validation.
3. Do not re-enable prompt-only enforcement.
4. Activity start rollback can remove individual explicit-start alternatives, but must keep the `is_activity_start_request()` boundary in both `continuation_candidate()` and `plan_from_request()`.
