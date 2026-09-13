# 画面判断をナレーション経路から分離しSTT hotword混入を修正

## 観測した原因

`logs/neuro_voice.log`の21:20〜21:23では、料理・次の行動を尋ねたturnが
`Fresh current-screen answer ready`の直後にLLM会話生成を通らずTTSへ入り、
visionの`summary`である「夜の森の中で雨が降っており…」をそのまま読んでいた。
21:23の「今は持ってるアイテムで焚き火とか作れる？」も同じ経路へ入り、
`inventory_open: True, menu_open: True`を含む内部状態を回答として読んでいた。

同turnのfinal STTは、質問本文の後ろに
「ペッポ チビ ゲスト4 日本語しりとり」を付加していた。調査すると
`Mind.speech_recognition_context()`がactive personaに加えて登録話者全員と、
COMPLETED状態のActivity名までdecode-time hotwordsへ渡していた。これは
TranscriptRepairer自身の「長いhotword listは語を挿入する」という契約に反していた。

## 実装

### 視覚質問

- `wants_current_visual_answer`: 最新frameが必要か。
- `wants_direct_visual_answer`: structured visionだけで安全に答えられるscene/OCR/state質問か。
- `needs_visual_reasoning`: 「今」「持っている」等の現在根拠と、作成可否・次の一手・攻略判断を同時に求めるか。

scene/OCRの明示質問だけは従来どおり一回のvision結果を短く直接回答する。
可否・材料・攻略・助言は最大6.5秒fresh frameを待ち、その観測をofficial
Minecraft knowledgeと通常Conversation Planner/LLMへ渡す。同じ`response_id`では
`context_for_turn`が二回目のvision inferenceを開始しない。

`_format_fresh_observation`は`player_state_details`を日本語ラベルへ変換し、
`inventory_open`、`menu_open`、raw `True/False`を出力しない。

### STT

- decode biasの話者は`current speaker`だけ。
- Activity名は`ActivityStateManager.active == true`の時だけ。
- fixed `initial_prompt`から旧名「ポチ」と例文を除去。
- fixed `hotwords`を空にし、turn contextだけを利用。
- bias上限を8から4へ縮小。
- final beamを5へ変更。
- 三個以上のexact context hotwordだけからなる短い末尾listは
  `context_bias_echo_removed`として除去。一個の名前を呼んだ普通の文は変更しない。

## テスト

- 関連: 72 passed。
- 全体: 738 passed / 1 failed、47.01秒。
- 既知失敗:
  `tests/test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target`
  （longer Discord historyをtargetに期待する既存テスト。今回の変更対象外）。

## 実機受入

1. GUIを再起動する。
2. 実マイクで「今は持ってるアイテムで焚き火とか作れる？」を数回発話する。
3. final transcript末尾へpersona/speaker/activity一覧が付かないことを確認する。
4. OBS Minecraftを見せ、インベントリを開いて同じ質問をする。
5. UI previewが質問時frameで、回答が情景説明ではなく「作れる/不足/不明」と
   必要材料・次の一手を答えることを確認する。
6. `inventory_open`、`menu_open`、`True`が画面にもTTSにも出ないことを確認する。
7. 「今何が見える？」は直接短文、「次はどうすれば？」は通常会話LLMの応答に
   なることをログのLLM初回指標で確認する。
8. Discord VCから同じ質問を行い、Localと同じroutingになることを確認する。

## 残る制約

Gemma 4 12Bの視覚推論は実ログで約4.5秒、GPU queue時は約10秒だった。
6.5秒で終わらない時は、古い観測を現在と断定せず、時刻付き直近状態と公式DBで
通常会話を続ける。小さいinventory icon/countをvisionが確信できない場合、
作成可否は不明として答えるべきであり、推測で埋めない。
