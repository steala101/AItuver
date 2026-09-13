# リアルタイム応答のP0設計

## LLM開始をブロックしないもの

STTの最終結果が得られたら、話者識別と音声感情認識を並行開始します。話者識別は `realtime.speaker_context_wait_ms`（既定120ms）だけ待機し、期限を超えた場合は unknown のままLLMを開始します。タスクはキャンセルしないため、完了した識別結果は以降のターンで利用されます。

Mindコンテキストは、現在の人格・関係性・User Modelだけを含む高速版を `realtime.context_deadline_ms`（既定150ms）以内に作ります。長期記憶の意味検索は、この最初のLLM開始経路から外しています。

## Turn Manager

`neuro_voice.realtime.TurnManager` が次の状態を明示的に記録します。

```text
IDLE -> USER_SPEAKING -> USER_PAUSED -> STT_FINALIZING -> AI_THINKING -> AI_SPEAKING
                         |                                  |
                         +-- USER_BACKCHANNEL ---------------+
                         +-- BARGE_IN_PENDING -> BARGE_IN_CONFIRMED
```

短い相槌は `USER_BACKCHANNEL` を経由してAIの再生を維持し、内容のある発話だけが `BARGE_IN_CONFIRMED` となり、フェードアウトと応答キャンセルへ進みます。

`turn_state` イベントでUIへ状態遷移を通知し、`latency` イベントには Context/Speakerの時間と期限超過を追加しています。
