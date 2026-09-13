# Local micからのDiscord画面共有ルーティング修正

- Date: 2026-07-26 19:40 JST
- Agent: Codex
- Status: completed（自動テスト済み、OBS実機受入待ち）

## 症状

トップ画面で「画面共有を見て」と話しても、OBSの`Discord画面共有`ではなくmonitor 1がUI previewとLLMの視覚入力に使われた。

## 原因

実ログでは発話sourceが`local:mic`だった。画面共有の開始・停止とOBS context注入は`DiscordBridge`にだけ接続され、`VoicePipeline`には接続されていなかった。さらに通常の`video.enabled`がfalseだったため、Localの明示的な視覚質問は`_capture_frame()`へ進み、`vision.monitor: auto`のmonitor 1を取得した。

## 変更

- `VoicePipeline`にもlazyな`DiscordScreenShareSession`を追加した。
- Local final発話で開始/停止commandをActivityや通常LLM生成より前に処理する。
- session active中は`_build_messages()`の先頭でOBS共有contextを注入し、通常のgame/video/monitor取得へ進まない。
- 最新OBS frameをUI previewへ送るが、会話履歴へraw imageは保存しない。
- Local voice loop終了とGUI shutdownの両方でsessionをcloseする。
- vision debug statusはactiveな共有sourceを優先表示する。
- `DiscordScreenShareSession`をLocal/Discord共通のconversation sourceとして文書化した。

## テスト

実行:

```powershell
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages').Path
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_discord_screen_share.py
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_discord_screen_share.py tests\test_realtime_vision.py tests\test_conversation_kernel.py tests\test_conversation_generation.py tests\test_conversation_features.py
```

結果:

- 専用5件成功
- 関連回帰54件成功
- 新規回帰はLocalの明示開始commandがsessionへ届くこと、普通の雑談ではidle sessionを生成しないこと、activeな共有sessionでmonitor captureが呼ばれずOBS contextとOBS previewが使われることを確認

## 実機受入

1. GUIを再起動する。
2. OBSでDiscord共有を捕捉するsourceを表示し、名前を設定の`Discord画面共有`と一致させる。
3. トップ画面のローカルマイクで「画面共有を見て」と言う。
4. UI previewがmonitor 1全体ではなくDiscord共有映像であることを確認する。
5. 「今どうなってる？」を数回行い、同じOBS sourceの最新状態に基づくことを確認する。
6. 「画面共有を見るのやめて」で停止し、その後の通常画面質問が通常のvision sourceへ戻ることを確認する。

## Rollback

`VoicePipeline`の`_screen_share_session_obj`、Local control処理、`_build_messages()`の優先branch、cleanupを削除し、`discord_share.py`の`latest_frame` propertyを戻す。ただしrollbackするとLocal micからの画面共有指示は再びmonitor 1へfallbackする。
