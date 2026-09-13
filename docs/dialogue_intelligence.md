# 対話インテリジェンス層

`neuro_voice.dialogue.DialogueIntelligence` は、LLMの前に置く軽量なリアルタイム対話制御層です。状態は `data/dialogue_<persona>.json` にペルソナごとに保存され、通常の長期記憶 DB とは分離されます。

## 実装した構成

| 要素 | 実装 |
| --- | --- |
| User Model | 話者ごとのターン数、発話量、質問頻度、間投詞傾向、最近の話題 |
| Relationship Manager | 既存の話者識別・なかよし度をそのまま応答方針へ反映 |
| Emotional Memory | openSMILE/SpeechBrain の高確信度（既定 0.75 以上）の感情を時系列保持 |
| Conversation Momentum | 発話間隔、長さ、質問性から 0.05–0.98 の会話継続度を推定 |
| Curiosity Engine | 連続質問を避け、話題に沿う場合だけ一つの自然な問いかけを許可 |
| Predictive Planner | 最近の話題から次の関連話題を提示。先回りの検索はしない |
| Confidence Manager | 音声感情・想起記憶・推測を混同しないよう、低確信の内容には留保表現を指示 |
| Memory Importance Scoring | 0–100 点。好み、継続中の活動、健康、明示的に重要な情報を優先し、一過性の出来事は保存しない |
| Internal Thought Layer | 推論の文章やチェーン・オブ・ソートを保存・表示せず、短い観測・応答方針だけを LLM に渡す |
| Plugin Scheduler | 検索の重複呼び出しを抑止し、既存の検索意図判定を通った要求だけを実行 |

## 応答までの流れ

```text
音声/STT/感情認識
  -> Turn Manager
  -> Mind + DialogueIntelligence
       -> User Model / 関係性 / 感情履歴 / Momentum
       -> Confidence / Curiosity / 予測話題 / ツール可否
  -> LLM 応答計画
  -> Style Manager -> VOICEVOX
```

この層は、検索・記憶・感情を「事実」として断定するためのものではありません。根拠が弱い場合は会話の口調を少し調整するか、確認を求めるために使います。

## 設定

`config/config.yaml` の `dialogue` で、有効化、高確信度とみなす感情の閾値、AIから質問する間隔、長期記憶に残す最低重要度を調整できます。
