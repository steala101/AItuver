# AI作業Handoff

- 担当AI: Codex
- Role: investigation / implementation / test
- Task: 「画面見てる？」で以前の画面が「いま見た画面」として表示される不具合
- Started At: 2026-07-26 22:01 JST
- Finished At: 2026-07-26 22:09 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: shared workspace

## 目的

明示的な現在画面質問では質問時のOBS frameだけを解析・表示し、通常会話や取得失敗時に以前の画像を現在画像として再表示しない。

## 調査結果

- `logs/neuro_voice.log`の22:01:30付近では「今、画面見てる？」のturnに`Fresh obs capture`が存在しなかった。
- `is_explicit_vision_query()`に「画面見てる」がなく、通常会話経路へ落ちていた。
- Local `VoicePipeline._build_messages()`とDiscord `_generate_and_speak()`は、active screen-shareの`latest_frame`を毎turn UIへ再送していた。
- `answer_current_visual_question()`は`last_explicit_frame`が現在request由来か検証していなかった。
- 新規明示取得に失敗しても、以前の`last_explicit_frame`が残る余地があった。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/vision/service.py` | 「画面見てる/見えてる」等を明示視覚質問へ追加 | 質問時fresh captureを起動する |
| `neuro_voice/vision/video.py` | 取得前の旧frame消去、`explicit_request_id`付与 | 失敗時fallbackとturn混同を防ぐ |
| `neuro_voice/vision/discord_share.py` | request ID一致frameだけpreview送信 | 画像証拠を現在turnへ固定する |
| `neuro_voice/pipeline.py` | 通常Local message buildのframe再送を削除 | queued frameの「いま見た」偽装を防ぐ |
| `neuro_voice/discord_bridge/bot.py` | 通常Discord message buildのframe再送を削除 | surface間で同じ鮮度意味論にする |
| `tests/test_discord_screen_share.py` | 一致/不一致と通常再送禁止を検証 | UI側の回帰防止 |
| `tests/test_realtime_vision.py` | 語句分類、取得失敗時clearを検証 | capture側の回帰防止 |

## 設計判断

- 「最新」はqueue上で一番新しいことではなく、その質問の後に取得され、同じrequest IDへ結び付いたことと定義する。
- UI previewの所有者はscreen-share sessionとし、通常Conversation message buildはraw frameを通知しない。
- 初回session previewは質問応答とは別のsource確認用途として維持する。

## テスト

| Command/Case | Result |
|---|---|
| `pytest tests/test_discord_screen_share.py tests/test_realtime_vision.py -q` | 33 passed |
| `pytest -q` | 740 passed / 1 failed |

全体の1 failureは未変更の既知問題:
`tests/test_speaker_merge.py::test_the_longer_history_is_offered_as_the_target`

## 設定・Migration

- Config: 変更なし
- DB schema: 変更なし
- Data migration: なし

## 残課題

- GUI再起動後、OBS sceneを明確に変えてから「今、画面見てる？」を発話し、同turnに`Fresh obs capture`が出ることを確認する。
- OBS取得不能時に以前のUI画像が再表示されないことを実機確認する。

## Rollback

上表の5 production fileにあるrequest ID検証、旧frame clear、通常frame再送削除を戻す。ただし戻すとstale/current混同が再発する。

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR updated: D-024
- Architecture/Codemap updated: あり
- Risk updated: R-020、R-034

## プライバシー確認

- `.env`値を記録していない。
- raw audio/imageを保存・添付していない。
- private transcriptを複製していない。

## 次の担当AIへの一文

画面鮮度の判定はtimestampや`latest_frame`ではなく、必ず`explicit_request_id == current response_id`を維持すること。
