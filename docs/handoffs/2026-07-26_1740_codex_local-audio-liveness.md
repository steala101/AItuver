# Handoff: local microphone and playback liveness

- Date: 2026-07-26 17:40 JST
- Agent: Codex
- Status: code and automated regression complete; GUI hardware acceptance pending

## Symptom and evidence

GUI起動後、マイク入力が届かず、ローカルTTSも聞こえなくなった。
`logs/neuro_voice.log`では17:23:06にマイク開始、17:23:45にTTS出力開始後、
17:23:46に`PortAudioError: Stream is stopped [PaErrorCode -9983]`で再生threadが終了していた。

根本原因は`VoicePipeline._device_watch_loop()`が3秒ごとの
`AudioDeviceMonitor.poll()`で、PortAudioのprocess-global
`_terminate()` / `_initialize()`を毎回呼んでいたこと。これは一覧更新だけでなく、
既存の入力・出力streamを停止する。

## Implemented

- 通常のdevice pollをread-onlyへ変更した。
- `MicCapture.active`は`_stream is not None`ではなく`stream.active`を返す。
- 停止時のPortAudio refreshは、ユーザー発話・応答・再生がidleの時だけ行う。
- `SpeakerPlayback`はwrite失敗時に同じPCM blockを最大3回reopen/retryする。
- 3回失敗時はchunkをpartial完了にしてturnを解放し、workerは次のTTSを処理し続ける。
- workerが予期しない例外で抜けてもsupervisorとenqueue時liveness checkで復旧する。

## Verification

- 実運用Python 3.11:
  - `LocalAudioLivenessTests`: 4件成功
  - `TurnClosureTests`: 9件成功
  - `GameProfileTests`: 5件成功
  - 合計18件成功
- `compileall -q neuro_voice tests`: 成功
- `run.py --list-devices`:
  - default input: Razer BlackShark V2 Pro
  - configured `audio.ai_output_device=9`: Razer BlackShark V2 Pro

## Hardware acceptance

GUI再起動後に次を確認する。

1. 30秒以上待ってもマイクレベル表示が動き続ける。
2. 2回以上連続で話しかけ、両方をSTTが処理する。
3. 2回以上連続のTTSがRazerから聞こえる。
4. `logs/neuro_voice.log`に新しい`再生スレッドでエラー`または
   `Stream is stopped`の未復旧終了が出ない。

## Rollback

本変更だけを戻すと3秒ごとのPortAudio全停止が再発するため、設定でのrollbackは推奨しない。
緊急回避は`audio.device_watch_interval_s: 0`でdevice watcherを無効化できるが、
起動後の機器接続自動認識も無効になる。
