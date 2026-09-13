# Discord共有とOBSゲーム映像のソース分離・切替

- Date: 2026-07-26 20:15 JST
- Agent: Codex
- Status: completed（自動テスト済み、Discord/OBS実機受入待ち）

## 症状と実ログの判断

- Discord共有のOBS frameは取得できていたが、既定`video.capture_max_width: 640`を継承していた。
- Discord画面の中に小さく表示された共有映像をさらに640pxへ縮小し、文字と細部が潰れていた。
- 汎用のDiscord共有sessionが`video.game_profile: minecraft`も継承し、Granblue等の映像へMinecraft専用instructionを適用していた。
- 「OBSのマイクラの画面を見て」はactive共有sessionのsource切替commandではなく通常会話へ流れていた。
- active session中の「画面の中の文字を読んで」は、複数人VCのaddressee推定で見送られる場合があった。

## 実装

1. `DiscordScreenShareSession`へ`source_kind`を追加した。
   - `discord_screen_share`: `discord.screen_share.obs_source_name`、汎用profile、1280px。
   - `minecraft_obs`: `discord.screen_share.minecraft_source_name`または`video.obs.source_name`、Minecraft profile、1024px。
2. 「OBSのマイクラの画面を見て」を決定的なsource切替commandとして扱う。
   - 旧service/解析を停止し、新sourceの初回previewとcold analysisを開始する。
3. Discord共有sourceが空の場合、通常ゲームsourceへ暗黙fallbackしない。
4. 共有解析の出力上限を240 tokensへ増やし、ゲーム名・状況・画面内文字のJSON切れを抑える。
5. 「文字を読んで」「何て書いてある」を明示視覚質問へ追加し、active requesterからの質問はgroup addressee判定より優先する。
6. LLM内部contextへ、`visible_text`があれば直接引用し、見えているのに質問をはぐらかさない制約を追加した。
7. UI previewへsource kind/nameを付け、OBS visualは最大520pxで表示する。クリック拡大は従来どおり。
8. OBS sampling logへ選択sourceと実frame寸法を追加した。

## 設定

```yaml
discord:
  screen_share:
    obs_source_name: Discord画面共有
    capture_max_width: 1280
    max_output_tokens: 240
    minecraft_source_name: null
    minecraft_capture_max_width: 1024
```

`minecraft_source_name: null`は通常の`video.obs.source_name`を使う。Discord共有側は未設定時にfallbackしない。

## 検証

```powershell
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages').Path
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m py_compile neuro_voice\vision\discord_share.py neuro_voice\vision\service.py neuro_voice\vision\video.py neuro_voice\pipeline.py neuro_voice\discord_bridge\bot.py
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_discord_screen_share.py tests\test_realtime_vision.py tests\test_conversation_kernel.py tests\test_conversation_generation.py tests\test_conversation_features.py
```

- 57 passed。
- source切替、旧service停止、profile/width分離、OCR質問、共有source未設定、旧frame metadata互換を確認。

## 実機受入

1. GUIを再起動し、OBSの`Discord画面共有`をcanvasいっぱいにfitする。
2. Discordから「Discordの画面共有を見て」と言う。
3. UI previewに`Discord画面共有 — Discord画面共有`と表示され、実際の共有映像が十分大きいことを確認する。
4. 「画面の中の文字を何か読んで」で、画面に実在する文字へ直接答えることを確認する。
5. 「OBSのマイクラの画面を見て」と言い、previewラベルが`OBSマイクラ画面 — <設定source>`へ変わることを確認する。
6. Minecraftの現在状態を質問し、Discord共有の古い解析を答えないことを確認する。
7. ログ`OBS realtime sampling: source=... size=...`で選択sourceと寸法を確認する。

## Rollback

`DiscordScreenShareSession`のsource kind/selectorとsource別overlay、UIのsource label、configの4項目を戻す。ただしDiscord共有が640px/Minecraft profileへ戻り、今回の誤認・OCR不良が再発する。
