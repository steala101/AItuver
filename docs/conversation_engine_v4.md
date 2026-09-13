# 会話エンジン v4 — 個性・創造性・関係性・自己成長

## 目的

v4 は、任意機能を回答後へ飾り付ける方式ではなく、`ConversationPlanner` が直接採否を決める方式へ変更した。通常は0〜2件、会話の勢いが高い場合だけ最大3件を採用する。選択は純粋な乱数ではなく、意図、感情、会話温度、関係性、記憶、直近の重複、学習済み重み、リスクから決まる。

追加のLLM呼び出しは行わない。既存の一回の応答生成へ、構造化された計画と表現規則を渡す。

## 実行フロー

```text
STT / 感情 / 話者
  -> ConversationOrchestrator
  -> Mind（時間・関係性・記憶検索）
  -> DialogueIntelligence
      -> ConversationPlanner
          -> Topic candidates
          -> ConversationFeatureSuite
          -> Candidate evaluation / 0〜3件を採用
      -> SurfaceRealizer
  -> LLM（一回）
  -> TTS
  -> ConversationCritic / 観測可能な学習
```

Planner と Surface Realizer を分離しているため、「何を話すか」と「どう話すか」を個別に改善できる。Critic は次ターンの計画へ短い修正規則を返し、同じ開始、語尾、質問過多、長文化、AI側視点の弱さ、断定過多を抑える。

## 任意機能

| 設定キー | 役割 | 主な安全条件 |
|---|---|---|
| `humor_engine` | 軽いツッコミ、小ボケ、比較 | 深刻・低気分・訂正時は抑止。直近パターンを反復しない |
| `casual_conversation_engine` | 横展開、過去話題、未解決話題の自然な再利用 | 確定記憶だけを参照。質問だけで維持しない |
| `story_engine` | 1〜3文のmicro story、説明の場面化 | 創作・シミュレーションとして扱う |
| `imagination_engine` | 仮説、別案、共同思考 | 推測表示。最新情報が絡む場合は検証要求を付ける |
| `playful_fantasy_engine` | 明示的な妄想・世界観遊び | 現実と混同させず、深刻時は抑止 |
| `world_knowledge_engine` | 内部・ローカル・検索済み知識の解釈 | 最新性が必要なら検証要求。記憶質問を検索へ送らない |
| `self_growth` | 好奇心、反応学習、一般化 | 単発で一般化しない。小さいEMA更新 |
| `relationship_evolution` | 相手別の距離感、率直さ、共有文脈 | 事実・安全・同意判断は変えない。急激に親密化しない |
| `value_system` | 誠実さ等の安定したAI側見解 | ユーザー迎合を避け、単発反応では値を変えない |
| `experience_memory` | 会話中の成功・失敗をエピソード化 | 実体験の捏造禁止。明示的反応だけを検証済み会話として保存 |

## データ

ペルソナごとに `adaptive_<persona>.db` を作り、次の論理テーブル種別を `adaptive_records` へ保存する。

- `ai_value_profile`, `ai_preferences`, `ai_active_curiosities`
- `ai_experience_episodes`, `ai_generalized_lessons`
- `relationship_states`, `relationship_events`
- `conversation_feature_usage`, `conversation_feature_feedback`
- `humor_pattern_history`, `topic_lifecycle`, `unresolved_topics`
- `candidate_evaluations`, `critic_results`, `conversation_learning_updates`
- `conversation_turn_traces`

全レコードに出典、確信度、重要度、期限、user ID、session IDを持てる。低重要度データ、期限切れ好奇心、古いデバッグ履歴は `prune()` で忘却できる。SQLite接続は操作時だけ開くため、Windows終了時やペルソナ切替時にファイルをロックし続けない。

## 学習制限

- 暗黙反応は学習率0.045、明示反応でも0.18のEMAに制限する。
- Plannerで選択しただけでは学習しない。最終発話へ反映された可能性を観測できた機能だけを対象にする。
- 一般化と価値観の微調整は、同方向の明示反応が3回以上ある場合だけ行う。
- 価値観の一回の変更幅は最大0.01。ユーザーの好みとAIの価値観は別レコードで管理する。
- 架空の経験を事実として保存しない。創作は `fiction`、仮説は `inference`、会話上の明示反応は `verified_conversation` とする。

## 設定とA/B確認

設定画面の「機能」タブから次を変更できる。

- 比較バリアント: `baseline / enhanced / experimental`
- 表現強度: `conservative / normal / expressive / debug_showcase`
- 10機能の個別ON/OFF
- 安全条件を維持した機能の強制テスト
- 会話エンジン開発者表示

`baseline` は任意機能を採用せず、候補評価だけを残す。`enhanced` と `experimental` は任意機能を採用する。開発者表示をONにすると「こころ」に、意図、感情、温度、関係性、取得記憶、好奇心、提案・採用・却下、候補評価、Critic、学習、品質指標、Planner時間、A/B概要が表示される。hidden reasoningやchain-of-thoughtは保存・表示しない。

## 品質指標

直近会話から、同一開始率、同一語尾率、質問終了率、連続質問率、直前応答類似度、平均発話長、機能採用率・最終発話反映率・却下率、肯定反応率、記憶利用率、ユーモア発動率、Planner処理時間を算出する。

## テスト観点

`tests/test_conversation_features.py` は、深刻時のユーモア抑止、機能トグル、baseline比較、最大採用数、DBスキーマと忘却、段階的学習、一般化条件、価値観の変化上限、開発者スナップショット、人物別関係性の永続化を検証する。既存の `tests/test_interaction.py` は、User Model、感情信頼度、Tool Scheduler、内部コンテキスト非露出を引き続き検証する。
