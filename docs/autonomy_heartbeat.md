# 自律行動Heartbeat

自発発話は、従来のランダム自発モードではなく `autonomy` の専用
Heartbeatで再評価します。Heartbeat自体はLLMやTTSを呼ばず、OpenThreadなどの
候補を軽量に採点するだけです。`DO_NOTHING` と `WAIT` は正常な結果であり、次の
tickを止めません。

標準設定は20秒ごとです。ローカル音声とDiscordは別々のHeartbeatを持つため、
Discord中はローカルの自発音声が混ざりません。

主な設定は `config/config.yaml` の `autonomy` セクションです。

- `heartbeat_enabled`: Heartbeatの有効化。
- `heartbeat_interval_ms`: 再評価間隔（標準20,000ms）。
- `silence_*_ms`: 短い/中程度/長い沈黙の判定境界。
- `group_idle_min_silence_ms`: グループ通話で発話候補を検討し始める最短沈黙。
- `min_speech_utility`: LLMを使う自発発話の最低効用。

人の音声がVADで始まると、未開始のHeartbeat結果と進行中の自発生成を取り消します。
自発TTSは再開せず、通常の人間優先の会話処理へ渡します。

診断用には `autonomy_heartbeat`、`autonomy_speech_cancelled`、
`autonomy_turn_finished` のイベントを出力します。Heartbeatのタスク生存、次回予定、
失敗数は `AutonomyHeartbeatScheduler.health_status()` で確認できます。
