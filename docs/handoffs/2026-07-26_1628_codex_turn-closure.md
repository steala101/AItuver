# Handoff: 自然な会話終了と短応答

- Date: 2026-07-26 16:28 JST
- Agent: Codex
- Status: code/tests completed, real audio acceptance pending

## User intent

ポッポが一対一会話でも全ユーザー発話へ長文を返す挙動を止める。説明後の相槌、理解、軽い感想で会話が自然に終わるなら沈黙し、返すとしても短くする。質問・依頼・遊びまで無視してはいけない。

## Root cause

1. `AddresseeDetector`は一対一を会話継続としてrespondへ寄せる。
2. `ResponsePlanner`の終了相槌は少数の完全一致だけだった。
3. 複合相槌はLLMへ届き、既存promptだけでは説明・質問の追加を止められなかった。
4. final発話は判定前に`active_topic`へ入り、「なるほど」が話題を上書きした。

## Implemented

- `TurnClosurePolicy`をLLM前に追加し、`CONTINUE / SILENCE / BRIEF_ACK`を決める。
- feedback-onlyの複合表現を認識する。
- 質問、訂正、依頼、actionable yes/no、canonical Activity入力を終了扱いから除外する。
- 沈黙時にconversation floorを閉じ、直前の意味ある話題を復元する。
- 一対一または明示的な感謝だけはdirect reply「どういたしまして。」を使い、LLMを呼ばない。
- Local/Discord双方が同じ`ConversationOrchestrator`判断を使う。
- GUI/ログeventへ`plan_reason`と`settles_exchange`を追加した。

## Invariants

- partial STTではclosureを確定しない。
- silent outcomeは生成失敗ではない。
- ActivityStateManagerが有効な間、短い入力をclosureが横取りしない。
- 質問・訂正・依頼をfeedback語が含まれるだけで沈黙へ落とさない。
- 終了相槌を会話の新しいtopicとして残さない。

## Config and rollback

```yaml
conversation:
  turn_closure:
    enabled: true
    max_feedback_chars: 48
    brief_reply_on_gratitude: true
```

緊急rollbackは`enabled: false`。DB migrationはない。

## Verification

Bundled Codex runtime:

```text
python -m py_compile <modified python files> -> success
python -m unittest tests.test_interaction.ConversationAddressingTests tests.test_turn_closure -v
Ran 32 tests: OK
```

全体の`unittest discover`も実行し、読み込めた290件は成功、3件skipだった。20 test moduleはbundled Codex runtimeに`pytest`、`yaml`、`requests`がないためimport errorとなった。変更に起因するassert failureはなかったが、正式なアプリPython環境でのfull pytestは未実施。

新規case:

- 複合理解と感想 -> silence
- silence後のtopic復元
- 質問・訂正・依頼 -> respond
- Activity中の「うん」 -> respond
- 「流す？」への「うん」 -> respond
- 明示的感謝 -> direct tiny reply
- Local `force_response=True`でも自然なsilence

## Remaining / acceptance

- 実機で`conversation_decision.plan_reason`を観測する。
- false-silenceとover-responseを別集計する。
- 「うん、それ面白い」のような言い回しを増やす際は、語彙追加だけでなく質問・依頼guardのreplayも追加する。
- グループVCでは別人の短い発話をActivity入力と誤認しないか確認する。

## Files

- `neuro_voice/dialogue/turn_closure.py`
- `neuro_voice/dialogue/planner.py`
- `neuro_voice/dialogue/state.py`
- `neuro_voice/dialogue/orchestrator.py`
- `neuro_voice/pipeline.py`
- `neuro_voice/discord_bridge/bot.py`
- `neuro_voice/mind/mind.py`
- `config/config.yaml`
- `tests/test_turn_closure.py`
- `tests/test_interaction.py`
- architecture/codemap/test/decision/risk/change documents
