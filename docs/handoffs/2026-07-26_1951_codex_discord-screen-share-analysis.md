# Discord画面共有の初回解析・プレビュー・宛先判定修正

- Date: 2026-07-26 19:51 JST
- Agent: Codex
- Status: completed（自動テスト済み、Discord/OBS実機受入待ち）

## 実ログから確認した事実

- `19:41:00` OBS WebSocket接続成功、source=`Discord画面共有`。
- 1fps取得は継続し、frame countは増加していた。
- 初回background visionは13,007ms、first tokenは10,395ms。
- 質問側は2.5秒で`wait_for(analyze_current_frame())`をtimeoutし、進行中解析をcancelして再開始していた。
- Discord surfaceは`vision_frame`をemitしておらず、ユーザーが実sourceを確認できなかった。
- 画面共有commandの一部はgroup addressingで`not_addressed`としてLLM/controller到達前に破棄された。

## 修正

1. `VideoObservationService.ensure_current_analysis()`
   - foreground/backgroundの進行中analysisを共有する。
   - `asyncio.shield()`により会話wait timeoutで推論taskをcancelしない。
   - timeout時は`pending=True`を返し、完了結果はPerceptionMemoryへ残す。
   - foreground中は二重background解析を起動しない。
2. 画面共有開始直後
   - acknowledgementと並行してcold vision解析を開始する。
   - first-frame eventでOBS実frameを約1秒後にUI previewへ送る。
3. Discord surface
   - 明示開始/停止commandはgroup addressingより優先する。
   - 各視覚質問でも最新OBS frameを`vision_frame`として表示する。
4. 状態文言
   - `frame未受信`、`受信済み・意味解析中`、`解析済み`を区別する。
   - 取得frame数、age、worker stateを内部contextに含める。

## 検証

```powershell
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages').Path
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_discord_screen_share.py tests\test_realtime_vision.py tests\test_conversation_kernel.py tests\test_conversation_generation.py tests\test_conversation_features.py
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m py_compile neuro_voice\vision\video.py neuro_voice\vision\discord_share.py neuro_voice\discord_bridge\bot.py neuro_voice\pipeline.py
```

- 関連55件成功。
- 変更4モジュール構文検証成功。
- 回帰は、短いtimeout後も同じanalysis taskが一件だけ継続し、二度目のwaitで完了結果を取得することを確認。

## 実機受入

1. GUIを再起動しDiscord VCへjoin。
2. Discordから「Discordの画面共有を見て」と指示する。名前呼び出しなしでもtool commandとして処理されること。
3. 約1秒後、チャットにOBS sourceの実previewが一枚出ること。
4. previewが目的の共有画面でなければOBS側のsource捕捉を修正する。
5. cold start直後は一度だけ「受信済み・意味解析中」になり得る。解析完了後「画面に何が書いてある？」へ具体的に答えること。
6. ログで同じ時間帯に複数のvision requestがcancel/restartされず、一件が完了すること。
7. 「画面共有を見るのやめて」で停止すること。

## Rollback

`VideoObservationService`のforeground task/event/ensure API、`DiscordScreenShareSession`のprime/preview、DiscordBridgeのtool addressing overrideとpreview emitを戻す。ただしrollbackすると初回visionが会話timeoutごとに再起動し、Discord UIからsourceを確認できない症状が再発する。
