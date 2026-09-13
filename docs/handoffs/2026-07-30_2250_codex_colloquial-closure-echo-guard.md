# Handoff: 口語終了相槌と短い導入をまたぐ重複発話の抑止

- Date: 2026-07-30 22:50 JST
- Agent: Codex
- Status: completed
- Scope: Local/Discord共通のTurn Closureとstreaming Reply Echo guard。

## User report

AIが「仕事があるって分かっていて粘るのはずるい、集中すると時間が溶けるから危ない」と返した後、
ユーザーが「ほんとそれ。」と同意すると、AIが短い導入だけ変えて同じ説明をもう一度発話した。

## Root cause

1. `TurnClosurePolicy`の口語同意に「ほんとそれ」がなく、内容を追加しない終了相槌がLLMへ進んだ。
2. `ReplyEchoGuard`は最初に確定した文だけを検査していた。
3. 最初の「あはは、やっぱり！」は短く、`containment(minimum_chars=8)`の比較対象にならなかった。
4. guardはその時点でstreamを解放したため、後続のほぼ同文本文を検査できなかった。

## Implementation

- `turn_closure.py`
  - `ほんとそれ / 本当それ / ほんとそう / 本当そう / まさにそれ /
    マジでそれ / マジでそう / それな / それね`を終了同意へ追加。
  - 質問、訂正、依頼、Activity入力、実行確認が先に`CONTINUE`となる既存順序は変更していない。
- `echo.py`
  - 通常の長さがある最初の文は従来どおり一文で判定する。
  - 最初が比較不能な短文なら、最大もう一文だけ保留する。
  - 結合した保留文と各実質文を既存source群へ比較する。
  - 重複時は保持中のUI token/TTS sentenceを一切解放しない。
  - 新内容なら二文をまとめて従来streamへ戻す。

## Verification

- Focused:
  - `tests/test_turn_closure.py`
  - `tests/test_reply_echo.py`
  - `tests/test_interaction.py`
  - `tests/test_silent_turn_directive.py`
  - Result: `193 passed in 4.70s`
- Full suite: `1143 passed / 2 skipped in 56.46s`
- `py_compile neuro_voice/dialogue/turn_closure.py neuro_voice/dialogue/echo.py`: success
- Skipは既存の`pyflakes`未導入のみ。

## Runtime acceptance

1. AIが二文程度の意見・説明を返した直後に「ほんとそれ。」と言う。
2. 新しい質問や依頼がなければAIは沈黙し、同じ説明を返さない。
3. 「あはは、そうだね！でも今やってるところだけ終わらせたい」のように新しい内容が生成された場合は、保持された短い導入を含め正常に発話する。
4. LocalとDiscord一対一の両方で同じ意味論になることを実機確認する。

## Remaining risk

- 未登録の口語同意はfixture追加が必要。
- 三文目以降から始まる意味的言い換えは、二文probeでは検出しない。
- 全文保留や追加LLMを採用していないため低遅延は維持するが、短い導入時だけ最大一文ぶんTTS開始が遅れる。

## Rollback

- Turn Closure語彙追加とEcho probeは独立して戻せる。
- Echo probeを戻しても従来の一文完全反復guardは残る。
- `conversation.turn_closure.enabled=false`で終了相槌の沈黙だけを無効化できる。

## Related

- ADR-0002
- D-033
- R-030
- R-043
