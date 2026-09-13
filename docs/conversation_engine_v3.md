# 会話生成エンジン v3

## 目的

質問に対する固定形の回答ではなく、ユーザー入力、関係性、感情、記憶、会話の勢いを使って、毎ターン理由のある会話展開を選ぶ。

## 実行経路

```text
User speech
  -> STT / emotion / speaker
  -> Turn & addressing policy
  -> Mind
       User Model
       Relationship / Momentum / Emotional Memory / Time Awareness
       Long-term memory recall
       Conversation Planner (what to say)
          -> 4 weighted topic/move candidates
          -> style, shape, AI emotion, temperature, rhythm
       Surface Realizer (how to say it)
  -> one streaming LLM generation
  -> EmotionTagParser
  -> Style Manager -> TTS
  -> Conversation Critic
       -> diversity feedback for the next turn
```

Planner、Surface Realizer、Criticを分離しているが、LLM生成は一回だけである。内部計画は決定論的な軽量処理なので、LLMを直列に複数回呼ぶ構成よりリアルタイム会話に向く。

## 永続状態

`data/dialogue_<persona>.json` のユーザー別領域へ次を保存する。

- 直近の会話スタイル
- 直近の応答構成
- 直近の返答
- 慣性を持つAI感情
- 最後のConversation Plan
- 最後のCritic評価

データはペルソナごと、かつ識別できた話者ごとに分離される。

## 候補の情報源

候補はランダム生成せず、次の情報源から最大4件を比較する。

1. 現在の発話
2. 言及可能な高関連長期記憶
3. ユーザーが明示した興味・プロジェクト
4. 直近の話題
5. 現在の状況に基づく別視点、予想、軽いユーモア

記憶候補は実際に検索された記憶だけを使用し、存在しない過去会話を作らない。

## 拡張方法

ユーモア、物語、想像、価値観などの将来モジュールは `ConversationCandidateProvider` を実装し、`ConversationPlanner.register_candidate_provider()` へ登録する。表現だけを追加する場合は `SurfaceRealizer.register_extension()` を使う。

追加エンジンが例外を出してもリアルタイム応答経路は継続する。候補Providerは話題と会話動作を提案し、最終文面を直接生成しない。

## 設定

`config/config.yaml` の `dialogue.conversation_generation` で制御する。

- `enabled`: 新しい会話生成層の有効・無効
- `candidate_count`: 比較する候補数
- `style_repeat_penalty`: 直近スタイルを避ける強さ
- `emotion_inertia`: AI感情が急変しない強さ

## 安全条件

- 内部候補、score、style名、分析過程は発話しない
- 記憶は高関連かつ言及可能なものだけを自然に使う
- 自然な言い直しは許可するが、事実や経験を意図的に捏造しない
- 不明な略語や事実へ迎合せず、知らない場合は明示する
- 支援・訂正場面ではユーモアを抑制する
