# Handoff: 自発発話を相互対話へ統合

- Date: 2026-07-30 00:57 JST
- Agent: Codex
- Status: completed

## User-visible symptom

自発発話が毎回「さっき言っていた件」「以前の疑問」の確認になり、内容も反復した。
ユーザーが返しても会話として接続せず、通常turnは一問一答、自発turnは脈絡のない独り言に
分離していた。

## Confirmed root causes

1. Localの`respond_text(internal_event=True)`はsystem以外の会話履歴を捨てていた。
2. Local/Discordとも自発生成後の本文を`ConversationManager`へassistant turnとして保存しなかった。
3. `_fresh_topic_candidate()`はactive theme、interest、recent topic、reflection等を全部LLMへ渡し、
   同じ顕著な材料を何度でも選べた。
4. 自発発話が2回続くとopen-thread recallを人工的に高得点にする`prefer_recall`があり、
   過去話題確認へ戻りやすかった。
5. 質問budget用のfieldは存在したが、候補選択と実発話記録に使われていなかった。

## Implementation

- `AutonomousActionSystem`
  - contextを個別focusへ分解し、未使用focus一件だけを選択。
  - 正規化、包含、bigram Jaccardで直近focusとの高類似を拒否。
  - focusを使い切った場合は候補を作らず`DO_NOTHING`。
  - `share_then_ask / observation / present_observation / share_thought`を選択し、
    質問は5分上限、cooldown、連続上限を適用。
  - 実際のreplyが質問だった時だけ質問budgetへ記録。
  - open-thread recallの人工的な優遇を削除。
- `ConversationManager.messages_for_autonomous_turn()`
  - 通常履歴を保持して内部指示をturn-localに付加。
  - 内部指示は履歴へ保存しない。
- Local/Discord
  - 実際の自発replyをassistant履歴へcommit。
  - LocalもConversationOrchestratorのresponse lifecycleへ自発turnを通知。
- Config
  - `autonomy.open_thread_callbacks_enabled: false`を既定化。

## Tests

- `python -m py_compile`（変更Python）: success
- 自発・heartbeat・履歴橋渡し: 21 passed
- 全体: 1116 passed / 2 skipped
- skippedは既存の`pyflakes`未導入のみ。

## Acceptance to run

1. Localで通常会話を数turn行い、30秒以上黙る。
2. 自発発話が具体的な短い意見または現在話題への一問になり、過去進捗確認にならないことを確認。
3. 質問へ普通に答え、ポッポが自分の質問を理解した続きとして返すことを確認。
4. 再び黙り、同じ話題・同じ入り方を繰り返さず、材料を使い切れば黙ることを確認。
5. Discord一対一でも同じ確認を行う。

## Remaining risks

- focus選択と履歴連続性は決定的に修正したが、12Bモデルの語彙・ユーモア・意見の質は実会話評価が必要。
- Local/Discordは別`AutonomousActionSystem` instanceのため、状態自体は共有されない。
- 既定でopen-thread callbackを止めたため、昔の用事を自動で尋ねる機能は弱くなる。必要なら設定で明示的に再有効化できる。

## Rollback

- 過去callbackだけ戻す: `autonomy.open_thread_callbacks_enabled: true`。
- focus選択を戻す場合も、`messages_for_autonomous_turn()`と自発assistant履歴commitは残すこと。
  ここを戻すと、ユーザー返答が自発質問へ接続しない元の断絶が再発する。

## Related

- D-029
- ADR-0002
- R-022
