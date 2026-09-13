# 「今」の画面質問を質問時フレームへ固定

- Date: 2026-07-26 20:36 JST
- Agent: Codex
- Status: completed（自動テスト済み、OBS実機受入待ち）

## 実ログで確認した原因

1. OBS captureは1fpsで`latest_seq`が増え、画面変化量も更新されていた。
2. 20:26台の「今見えてる景色はどんなの？」は、質問前に開始されたcold vision結果へjoinした。
3. structured visionは約13〜15秒、結果を受けた通常会話LLMも約10〜20秒かかった。
4. そのため回答時には質問時点より20〜30秒古い景色となり、さらにLLMが古い観測を「今」と言い換えていた。
5. 「もう一回見て」は明示視覚質問として認識されず、古いPerceptionMemoryだけで通常応答する場合もあった。

## 修正

- `VideoObservationService.ensure_current_analysis(force_fresh=True)`
  - cold/background/foregroundの旧解析をcancelする。
  - 質問時にOBSから新規captureする。
  - このframeを`last_explicit_frame`として保持する。
- `DiscordScreenShareSession.answer_current_visual_question()`
  - Local/Discord共通。
  - 質問時frameを最大18秒待ってstructured vision解析する。
  - 観測結果を通常会話LLMへ再投入せず、OCR・位置/状態・対処を短い発話へ直接整形する。
  - timeout/失敗時は過去観測を代用しない。
- `is_explicit_vision_query`
  - 「もう一回見て」「もう一度見て」「見直して」「撮り直して」「前の画面」「古い画面」を追加。
- 診断
  - `Explicit vision completed`へ`analyzed_seq`、`latest_seq`、質問frame age、scene distanceを出す。
  - UIへ質問時に解析した正確なframeを再表示する。
- latency
  - Minecraft structured outputを240から160 tokensへ戻した。
  - 二重12B推論を一回に削減した。

## 検証

```powershell
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages').Path
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m py_compile neuro_voice\vision\discord_share.py neuro_voice\vision\service.py neuro_voice\vision\video.py neuro_voice\pipeline.py neuro_voice\discord_bridge\bot.py
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_discord_screen_share.py tests\test_realtime_vision.py tests\test_conversation_kernel.py tests\test_conversation_generation.py tests\test_conversation_features.py
```

- 60 passed。
- old in-flight taskをcancelし、二つ目のfresh frameだけを回答へ使うことを確認。
- fresh structured observationが通常LLMを介さず直接回答になることを確認。
- malformed/truncated JSONの内部field名を発話しないこと、実config loaderを確認。

## 実機受入

1. GUIを再起動し「OBSのマイクラを見て」。
2. 画面を分かりやすく別の場所へ移動する。
3. 「今、どこにいる？」「もう一回見て」と質問する。
4. チャットに出るpreviewが質問時の画面であることを確認する。
5. 回答がそのpreviewと一致し、質問前の景色を現在形で答えないことを確認する。
6. ログで`Explicit vision completed`の`analyzed_seq`が質問時に新規取得された値であることを確認する。
7. 18秒超過時は旧景色を答えず、解析timeoutを明示することを確認する。

## 残る限界

現行のGemma 4 12B visionは単一frameでも実測約13秒かかる。今回の修正は「質問前の古いframeを使う」「二回LLMを呼ぶ」を除去するが、連続動画を1〜2秒遅延で意味解析するものではない。そこまで短縮する場合は、小型vision model、別GPU、Minecraft telemetry、OCR/物体検出の高速laneを比較する。

## Rollback

`force_fresh`、`last_explicit_frame`、`answer_current_visual_question`とsurface配線を戻す。ただし質問前のin-flight解析を現在として答える不具合と二重LLM遅延が再発する。
