# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: design / implementation / test
- Task: マイク／スピーカーの手動再認識機能の追加
- Started At: 2026-07-27 (JST)
- Finished At: 2026-07-27 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)
- Work Lock: 同セッション内で連続作業。他AIの同時編集なし

## 目的

ユーザー依頼「マイクが認識しなかった時に再認識させる機能を追加して」。実機検証の前段として、アプリを再起動せずに音声入出力を取り直せるようにする。

## 作業開始時の状態

- 直前に同セッションで沈黙ターン／話者ヒステリシスの修正を実施済み（`2026-07-27_0030` handoff）
- テストbaseline: 726 passed / 3 skipped

## 調査結果

既存の自動復旧（`pipeline._device_watch_loop`、Codexが2026-07-26 17:40に read-only 化）は、**一度開いたストリームが停止した場合**しか救わない。次の2つは救えない。

| 状況 | 自動復旧が効かない理由 |
|---|---|
| 起動時にヘッドセットが無く、後から接続した | PortAudioのデバイス一覧はキャッシュされる。`mic_down`は真になるが、refreshが走るのは停止検知後であり、そもそも一度も開けていない場合に指定デバイスが現れても設定名が一致しないと開けない |
| Windowsが同じ機器を別エンドポイントへ移した | 古いハンドルが`stream.active == True`を返し続けるため`mic_down`が偽のまま。監視は「変化なし」と判断する |

いずれも従来はアプリ再起動が唯一の対処だった。

自動監視が`refresh=False`固定である理由（PortAudioの`_terminate`/`_initialize`はプロセス全体のストリームを止める）は正しく、そこは変えていない。**明示操作のときだけ**一覧の作り直しを許す形にした。

### async/queue/cancel

- UI(pywebviewスレッド) → `asyncio.run_coroutine_threadsafe` → pipelineのイベントループ、20秒timeout。
- 一覧の作り直しとマイクのopenはいずれも `asyncio.to_thread`。イベントループを塞がない。
- 再生は`request_reopen()`で次チャンクから作り直す（既存の仕組み。キューは破棄しない）。

### privacy/data

- デバイス名のみを扱う。音声データ・個人情報を新たに保存・記録しない。
- 失敗メッセージは `safe_error_text` を通し、PortAudioの例外文字列に含まれるパスやURLを画面へ出さない。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/audio/capture.py` | `_open()` を分離。`start(allow_fallback=False)` / `restart(allow_fallback=False)`。`opened_device` / `used_fallback` を公開 | 指定デバイスを必ず先に試し、既定へ落ちたことを言えるようにする |
| `neuro_voice/audio/devices.py` | `device_label()` を追加 | 画面表示から `#hostapi` サフィックスを外す。比較用の値はそのまま |
| `neuro_voice/pipeline.py` | `redetect_audio_devices()` と `_rebuild_device_list()` を追加 | 一覧の再構築 → スナップショット → 再生reopen → マイク再open → 結果報告 |
| `neuro_voice/ui/webview_app.py` | `redetect_audio_devices()` をUI APIへ公開 | pywebviewスレッドからイベントループへ橋渡し |
| `neuro_voice/ui/assets/index.html` | 設定「通話の聞き取り方法」欄へ「🎤 マイクを再認識」ボタンと結果表示を追加 | ユーザー指定の設置場所 |
| `tests/test_audio_redetect.py` | 新規9件 | 下記テスト節 |

### 挙動

1. 再生ストリームへ reopen を予約する。
2. PortAudioのデバイス一覧を作り直す（**明示操作時のみ**）。
3. 現在の既定入出力を読む。
4. マイクを `allow_fallback=True` で開き直す。設定した指定デバイス → 見つからなければ既定デバイスの順。
5. 結果を返す: `ok` / `microphone` / `speaker` / `used_fallback` / `message` / `error`。
6. 既定へ落ちた場合は「設定したマイクが見つからないため既定デバイスで開きました（デバイス名）」と表示し、UIでは色を分ける（緑=指定どおり、ピンク=既定で代替、赤=失敗）。

**自動経路は既定へ落ちない。** `allow_fallback` は既定 `False` で、`_device_watch_loop` からは渡していない。黙って別のマイクへ切り替わるのは、開けないことより悪い。落とすのは人が押したときだけ。

## 変更しなかったもの

- `_device_watch_loop` の `refresh=False` 方針（Codexの2026-07-26 17:40の修正）。手動経路を足しただけで、自動監視の周期・条件は一切触っていない。
- `audio.device_watch_interval_s` などの設定値。
- Discord側の取り込み。今回は対象外（ユーザー選択「マイクとスピーカー両方」）。

## 設計判断

1. **自動と手動でポリシーを分ける。** 自動監視は「壊さない」ことが最優先なので read-only かつ指定デバイス厳守。手動は人が意図して押したので、一覧の作り直しと既定へのフォールバックを許す。同じ関数に両方の性格を持たせない。
2. **既定へ落ちたことを必ず言う。** 黙って動くと「直った」と誤解し、後で音が入っていないことに気づく。第1条の`INVARIANT`（能力を偽らない）と同じ考え方。
3. **表示名と比較値を分ける。** `#hostapi` サフィックスはWASAPI/MMEの同一機器を区別するので変化検知には必要。剥がすのは表示のときだけ。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 736 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_audio_redetect.py`（新規9件） | pass | 指定優先／既定フォールバック／自動経路は落ちない／表示名 |
| `tests/test_local_audio_liveness.py` + `test_audio_devices.py`（sounddeviceスタブ下で実行） | 32 passed | `start()`/`restart()`の引数追加が既存呼び出しを壊していないことを確認 |
| `python3 -m compileall neuro_voice` | exit 0 | — |
| `index.html` の要素・ハンドラ対応 | OK | `audioRedetect` / `audioRedetectStatus` / `redetect_audio_devices` / `loadOutputDevices` の出現数を確認 |

- 未実施: **実機GUIでの動作**。ヘッドセットを抜いた状態で起動→接続→ボタン、通話中の押下、指定デバイスを存在しない値にした場合の表示。
- `sounddevice`が無いsandboxではUI経路を実行できないため、pipeline側の`redetect_audio_devices()`自体は実機確認が必要。

## 設定・Migration

- Config: 変更なし。新規キーなし。
- DB schema: 変更なし。
- 再起動の要否: **不要**。ボタンはその場で効く。

## Rollback

`index.html` のボタンを削除すれば呼び出し経路が消える。Python側は追加のみで、`allow_fallback` の既定が `False` のため、既存の自動経路の挙動は本変更前と同一。

## 次の担当への注意

- `allow_fallback=True` を自動経路（`_device_watch_loop`、起動時の `start()`）へ広げないこと。ユーザーが選んだマイク以外が黙って使われる。
- `_device_watch_loop` の `refresh=False` を変えないこと。理由は `devices.py::AudioDeviceMonitor.poll` のdocstringにある（プロセス全体のストリームが止まる）。
- 実機受入項目: (a) ヘッドセット未接続で起動→接続→ボタンで認識するか、(b) 存在しないマイクを設定した状態でボタン→既定で開き、その旨が表示されるか、(c) 会話中に押しても再生が復帰するか、(d) 押した後にデバイス選択リストが最新化されているか。
