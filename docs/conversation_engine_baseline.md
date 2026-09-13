# 会話エンジン基盤: 現状と段階的統合

## 現在のクリティカルパス

```text
Discord VC
  -> discord_receiver/receiver.js
     (DAVE 復号、ユーザーごとの PCM、localhost WebSocket)
  -> neuro_voice/discord_bridge/direct_receiver.py
     (アカウントごとの VAD セグメント化、16 kHz 変換)
  -> neuro_voice/discord_bridge/bot.py
     (STT、声紋照合、音楽判定、LLM、TTS)
  -> Discord VC
```

ローカル会話は `neuro_voice/pipeline.py` が VAD/STT/TurnManager/LLM/TTS
を担当する。長期記憶・人格・声紋は `neuro_voice/mind/`、短期履歴と
中断した話題は `neuro_voice/memory/conversation.py` が保持する。

## 再利用する責務

- Node 側の DAVE 直受信、復号、PCM 化、および Python への IPC は変更しない。
- Discord アカウントは transport metadata のみであり、話者確定は Mind の声紋
  結果に委ねる。この分離を維持する。
- `TurnManager` は音声再生中の相槌・割り込みを制御する。
- `ConversationManager` は短期履歴と保留したトピックを持つ。
- `Mind` はペルソナ別の長期記憶、話者関係、感情履歴を持つ。
- `ContextAssembler` は Mind の補助情報を 150 ms の予算内で追加する。

## 現在の不足と統合方針

Discord の `_should_respond` は、ウェイクワードまたは直近応答からの経過時間を
使うだけで、複数人会話の宛先・話題・応答方針を構造化していない。そこで音声
経路の後段に次の軽量層を追加する。

```text
speech.final
  -> ConversationEvent / EventDispatcher
  -> ConversationState
  -> AddresseeDetector (同期ルール)
  -> ResponsePlanner
  -> 既存 LLM / Mind / TTS
```

曖昧時の LLM 判定は設定で任意にし、既定では同期ルールのみを使う。これにより
初回応答のクリティカルパスを増やさず、判断理由をログへ残せる。

## 段階と回帰リスク

1. **Phase 1**: この基準書と責務境界を追加する。
2. **Phase 2**: 共通イベント、Dispatcher、Conversation State を副作用なく追加する。
3. **Phase 3**: 宛先判定と Response Plan を Discord の応答ゲートへ接続する。
4. **Phase 4 以降**: Mind 想起スコア、保留発話の再開、自発発話を同じ状態へ接続する。

最大の回帰リスクは、人間同士の会話への誤応答と必要な応答の取りこぼしである。
そのため既定は保守的なしきい値にし、判断結果と理由をログ・UIイベントへ送る。
Node/DAVE、音声パケット処理、声紋 DB の形式は変更対象外である。
