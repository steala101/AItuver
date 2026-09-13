# AI作業Handoff

- 担当AI: Claude
- Role: design / implementation
- Task: 認知カーネルMVP（行動選択層）を最小の垂直スライスで通す
- Finished At: 2026-08-01 07:30 JST
- Work Lock: 単独編集

## 1. 再利用した既存機能

| 責務 | 既存 | 判定 |
|---|---|---|
| イベント | `dialogue/events.py` `ConversationEvent` | REUSE（アダプタで包む） |
| 会話手の点数付け | `dialogue/selection_kernel.py` | REUSE |
| 義務・参照解決・検索可否 | `dialogue/kernel.py` `TurnFrame` | REUSE |
| 何を話すか | `conversation_planner.py` | REUSE |
| どう言うか | `surface_realizer.py` | REUSE |
| 作業記憶・関係性・感情 | `working_memory.py` / `relationship.py` / `temporal_self.py` | REUSE |
| **行動型と行動選択** | なし | **NEW** |
| **結果を状態へ戻す経路** | なし | **NEW** |

「行動」という層だけが無かった。他は全部あった。

## 2. 追加・変更した主要ファイル

- `neuro_voice/cognition/types.py` — `CognitiveEvent` / `ActionType`(12) /
  `ActionCandidate` / `ActionDecision` / `ActionOutcome` / `InformationType`
- `neuro_voice/cognition/state.py` — `CognitiveState`（**読み取り専用の統合ビュー。
  値は持たない**）
- `neuro_voice/cognition/kernel.py` — `propose → decide → apply_outcome`、
  重みは `WEIGHTS` の1箇所
- `neuro_voice/mind/mind.py` — `cognitive_state()` / `record_cognitive_action()`
- `neuro_voice/pipeline.py` — `respond_text` へ配線、`finally` で結果を戻す
- `config/config.yaml` — `cognition.enabled`（既定 false）
- `tests/test_cognitive_kernel.py` — 26件

## 3. 完成した実行フロー

```
ユーザー発話
→ CognitiveEvent
→ mind.cognitive_state()   （関係性・感情・義務・作業記憶・聞き取りの確かさ）
→ kernel.propose()         （2〜5候補。危険/低信頼/割り込みは規則で）
→ kernel.decide()          （加重スコア。最終判断はコード）
→ ActionDecision           （ログとイベントへ）
→ 既存の Conversation Planner → Surface Realizer → TTS
→ ActionOutcome            （完了 / 中断 / 失敗）
→ apply_outcome()          （義務・中断発話・行動履歴・関係性の微小差分）
```

反射（VAD・割り込み・TTS停止）は**この経路を通さない**。待たせたら
割り込みが効かなくなる。

## 4. 実行したテストと結果

- `tests/test_cognitive_kernel.py` 26 passed（必須7シナリオを網羅）
- 全体 **1343 passed / 17 subtests**
- pyflakes 3 passed
- 配線確認: `respond_text` に決定と結果戻しの両方があり、`_on_speech_start`
  （反射）には無いことを実行時に確認

LLM非依存は**importとASTで**検証した。説明文に「LLM」と書いてあること自体は
問題ではないので、文字列検索では判定にならない。

## 5. 残っている重要課題

1. **決定がまだ効いていない。** `ActionDecision` はログとイベントに出るだけで、
   Conversation Planner の入力へ反映していない。次は
   `ActionType → response_goal / response_shape / social_mode` の対応付け。
   **ここを繋いで初めて挙動が変わる**
2. Discord 未配線（Local のみ）。第19条の parity は次段
3. `cognition.enabled: true` での実機確認。落ちても旧経路で続行するが、
   遅延の増分は測っていない

## 設計上、意図的にやらなかったこと

- 完全な世界モデル・自律エージェント化
- 感情の自己保存欲求（停止を避けるための行動）。**実装していない**。
  中断発話の保持と義務の退避だけを「システム継続性」として持つ
- 未使用の抽象クラス。`cognition/` の3ファイルはすべて呼ばれている

## 次の担当への一文

**次の一歩は `ActionType` を `response_goal` へ繋ぐこと。**
そこまでやらないと、選んだ行動が発話に届かない。
