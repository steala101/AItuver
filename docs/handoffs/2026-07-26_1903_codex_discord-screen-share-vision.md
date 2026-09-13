# Discord画面共有のOBS継続認識

- Date: 2026-07-26 19:03 JST
- Agent: Codex
- Status: completed / hardware acceptance pending

## 目的

Discord VCで「画面共有見て」と言うと共有映像を継続認識し、その後の自然な質問へ最新状態を使う。「見るのやめて」またはVC退出で確実に停止する。

## 実装

- `neuro_voice/vision/discord_share.py`
  - stop優先の明示command分類。引用・伝聞の「見てって言った」は実行しない。
  - `DiscordScreenShareSession`がOBS transport、Perception、VideoObservation lifecycleを所有。
  - session-local config overlayで`video.input=obs`とDiscord専用sourceを指定し、ローカル選択を変更しない。
  - 明示視覚質問はfresh frame解析を既定2.5秒だけ待つ。超過時はage付き直近状態を渡し、現在だと断定しない。
- `neuro_voice/discord_bridge/bot.py`
  - final発話からstart/stopをcontrol replyとして処理。
  - 通常Discord応答へ共有画面contextを注入。
  - VC leaveとprocess stopでsessionを停止・close。
- GUI/config
  - Discord設定に有効化とOBS source名を追加。
  - 既定sourceは`Discord画面共有`、1fps、1 analysis frame。

## OBS実機設定

1. Discordの共有画面をポップアウトする。
2. OBSに「ウィンドウキャプチャ」を追加し、そのDiscord共有ウィンドウを選ぶ。
3. OBS source名を`Discord画面共有`にするか、ポッポの設定 → Discordで同じ名前を指定する。
4. OBS WebSocketのhost/port/passwordは既存の機能タブ設定を使う。sourceは非表示にしない。
5. VCで「画面共有見て」→ 数秒後に「今どうなってる？」→「画面共有を見るのやめて」を確認する。

## 検証

- `compileall`: 変更Python成功。
- `pytest`: `test_discord_screen_share.py`、`test_realtime_vision.py`、`test_obs_capture.py`、`test_conversation_generation.py`、`test_turn_closure.py`の45件成功。

## 未完・注意

- DAVE/Go Liveの映像直接復号は未実装。現在のDAVE主経路は音声だけで変更していない。
- Ollama visionが2.5秒を超えた場合、そのターンは時刻付き直近状態を使用し、fresh解析は後続ターンへ反映される。
- OBSポップアウトのwindow identityが再生成される環境ではOBS側のWindow Capture追従設定を実機調整する。
- raw共有画像は文書・会話履歴へ保存しない。
