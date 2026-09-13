# Minecraft攻略質問の公式DB高速経路

- Date: 2026-07-26 20:56 JST
- Agent: Codex
- Status: completed（自動テスト・実DB回答検証済み、OBS実機受入待ち）

## 症状

- Minecraft OBS認識中に「この豚肉を焼くにはどうすればいい？」と聞くと、攻略方法ではなく海中など現在画面の説明を返した。
- 同turnはfresh visionに約11.3秒かかっていた。
- 次の「じゃあ次はどうすれば？」は現在画面を使うべきだが、約4.3秒のvision推論を必要とした。

## 根本原因

`is_realtime_game_query()`が`どうすれば`を広く現在画面質問として扱い、
`DiscordScreenShareSession.answer_current_visual_question()`がMinecraft公式knowledge
lookupより先に実行されていた。

実SQLiteをread-onlyで確認すると、active Java 1.21.6公式JAR由来のrecipeには次が存在した。

- 生の豚肉 → 焼き豚 / かまど
- 生の豚肉 → 焼き豚 / 燻製器
- 生の豚肉 → 焼き豚 / 焚き火

よって不足していたのはデータではなくroutingと材料側検索だった。

## 実装

1. `is_minecraft_knowledge_query()`
   - recipe、材料、作り方、焼き方、かまど等を安定攻略知識として分類する。
   - `次はどうすれば`は分類しない。
2. `search_recipes()`
   - 出力名だけでなく、`生の豚肉`から`豚肉`をaliasとして材料側から検索する。
   - 指定されたかまど/燻製器/焚き火を優先する。
3. `verified_recipe_answer()`
   - active Java client JARで確認できた調理recipeだけを短い自然文へ変換する。
   - 「肉の焼き方」のように種類が省略された場合も、公式の肉recipeに共通する操作だけを答える。
4. Local/Discord
   - Minecraft OBS session中は公式調理回答をfresh visionより先に実行する。
   - Localのactive Minecraft shareでも、調理以外のrecipe質問へ公式contextを残す。
   - `画面を見て`が明示された場合は視覚を優先する。
5. Vision
   - `どうすれば`を含んでも安定攻略知識ならfresh frameを取得しない。
   - `次はどうすれば`、`今どこ`は従来どおり質問時frameへ固定する。
6. latency
   - Minecraft OBS幅を1024pxから768pxへ下げた。
   - Discord共有/OCR用1280pxは維持した。

## 設定

```yaml
game_assistant:
  minecraft:
    direct_verified_recipe_answer: true

discord:
  screen_share:
    minecraft_capture_max_width: 768
```

DB schema migrationはない。

## 検証

```powershell
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages').Path
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m py_compile neuro_voice\games\minecraft_knowledge.py neuro_voice\games\__init__.py neuro_voice\vision\discord_share.py neuro_voice\pipeline.py neuro_voice\discord_bridge\bot.py
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest -q tests\test_minecraft_official_knowledge.py tests\test_discord_screen_share.py tests\test_realtime_vision.py tests\test_conversation_kernel.py tests\test_conversation_generation.py tests\test_conversation_features.py tests\test_interaction.py -k "minecraft or screen_share or current_screen or realtime or cooking or recipe or conversation"
```

- 絞り込み実行: 105 passed、104 deselected。
- 最終の関連7 test file全件実行: 210 passed。
- 最終config読込: `direct_verified_recipe_answer=True`、`minecraft_capture_max_width=768`。
- 実config + 実DBでJava 1.21.6 / 1345 recipesを使用。
- 実回答:
  - `生の豚肉をかまどの上段、石炭や木炭などの燃料を下段に入れれば、焼き豚になるよ。 燻製器ならもっと速いし、焚き火でも燃料なしで焼けるよ。`
- 初期化込み回答文確定: 292ms。
- raw image、Base64、private transcriptは文書へ保存していない。

## 実機受入

1. GUIを再起動し「OBSのマイクラを見て」でMinecraft sourceへ切り替える。
2. 「この豚肉を焼くにはどうすればいい？」と聞く。
3. 画面preview/fresh vision requestを新規発生させず、公式調理法を短時間で読み上げること。
4. 「じゃあ次はどうすればいい？」と聞く。
5. この質問だけ質問時の最新frameをpreviewし、現在状況に基づいて答えること。
6. 768pxでhealth/hunger/held item等の状況認識が維持されること。
7. Local micとDiscord direct音声の双方で同じ意味論になること。

## Rollback

- 正確性に問題がある場合:
  - `game_assistant.minecraft.direct_verified_recipe_answer: false`
- HUD認識が落ちる場合:
  - `discord.screen_share.minecraft_capture_max_width: 1024`
- routing patch全体を戻すと、攻略質問が`どうすれば`だけで12B fresh visionへ流れる不具合が再発する。

## 残課題

- 768pxの実画面精度とcurrent-screen queryのp50/p95は未計測。
- crafting tableの複雑な配置recipeは直接自然文化せず、引き続き公式contextを会話LLMへ渡す。
- 真の1〜2秒current visual理解には小型vision model、Minecraft telemetry、OCR/物体検出laneの比較が必要。
