# ADR-0002: Canonical Stateと世代ガードを生成文より優先する

- Status: Accepted
- Date: 2026-07-26

## Context

音声ストリーム、TTS、視覚推論、検索、ゲーム手は非同期に完了する。古い結果を無条件に採用すると、割り込み後の音声再出現、終了済みゲームへの手、古い画面への応答が発生する。LLMの生成文は確定状態の保存先ではない。

## Decision

ActivityはProposal-Normalize-Validate-Commitを通し、確定状態を`ActivityStateManager`が所有する。音声応答は`response_id`と`PlayedTextTracker`で世代を追跡し、cancel済み世代の成果物を全段階で破棄する。将来追加する非同期経路も同じ原則に従う。

会話生成も同じ原則を使う。`ConversationPlanner`が選んだ主会話行為、質問可否、
会話記憶の取得根拠は`ConversationContract`として`response_id`とsurfaceへ束縛する。
TTS直前とfinal reply確定時に同じ契約を検証し、stale plan、未計画質問、未根拠な
記憶・自己経験主張を次の音声・UI確定表示・履歴へcommitしない。

会話の反復も生成文のお願いだけへ委ねない。`TurnClosurePolicy`が内容を追加しない短い
終了相槌をLLM前に閉じる。生成へ進んだ場合は`ReplyEchoGuard`が表示/TTS前の冒頭を有界保留し、
短い感情導入の後ろに隠れた直前発話の反復まで検査する。重複はcommit前に破棄し、
新内容なら保留分を解放する。低遅延のため全文生成完了は待たない。

自発発話では、内部のautonomy指示を架空のuser turnとしてcommitしない。生成時だけ現在の
ConversationManager履歴へ一時的に加え、実際に再生された本文をassistant turnとしてcommitする。
自発候補のfocusと質問budgetは`AutonomousActionSystem`が確定し、同じfocusを使い切った後は
新しい根拠が入るまで`DO_NOTHING`を選ぶ。

## Consequences

- stale outputを機械的に拒否できる。
- すべての非同期境界でID伝播と検査が必要になる。
- transport別に重複する実装は共通runtimeへ統合する必要がある。

## Alternatives

- promptで「古い結果を使わない」と指示: race conditionを防げないため不採用。
- queue全消去のみ: 実行中taskと遅延結果を止められないため不十分。

## Related Files

- `neuro_voice/activity/engine.py`
- `neuro_voice/dialogue/conversation_contract.py`
- `neuro_voice/dialogue/conversation_planner.py`
- `neuro_voice/dialogue/turn_closure.py`
- `neuro_voice/dialogue/echo.py`
- `neuro_voice/autonomy/engine.py`
- `neuro_voice/memory/conversation.py`
- `neuro_voice/realtime/played_text.py`
- `neuro_voice/realtime/turn_manager.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
