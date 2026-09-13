# ラジオ停止・終了後の独り言暴走 修正引継ぎ

- Date: 2026-07-26 18:10 JST
- Agent: Codex
- Status: completed（自動テスト済み、実機受入待ち）

## 症状とログ上の原因

ユーザーが3分ラジオを途中で止めても区間が続き、終了後も同じ問いやラジオ開始が繰り返された。

`logs/neuro_voice.log`の2026-07-26 17:48〜17:53では次を確認した。

1. 「ラジオは一旦終わろう」が既存Directiveの停止として認識されなかった。
2. 「もうラジオは終わったよ」が`ラジオ`を含むため、別の300秒MONOLOGUEとして新規作成された。
3. active状態だけを止めても、解釈中plan、区間task、生成済み未再生音声がsurface側に残り得た。
4. Directive終了後もAutonomyが直前のラジオ文脈をsilence候補として再利用し、同じ発話を繰り返した。

## 実装

- `intent_plan.is_stop_request()`を追加・強化し、開始候補より先に停止を判定する。否定形「止めないで」は停止扱いしない。
- `plan_from_request()`は停止文からplanを作らない。
- `DirectiveRuntime._stop_for_user()`が、active Directiveの有無を問わず以下を一括取消する。
  - pending Intent-to-Plan task
  - active segment task
  - directive response IDのLLM生成
  - surfaceの未再生音声
  - Controllerのactive Directive
- `DirectiveHost.on_user_stop`をLocal/Discord双方へ接続した。
- surface callbackはAutonomy continuation、heartbeat、進行中自律発話taskを止め、`post_stop_quiet_seconds`を設定する。
- quiet中は`SILENCE_STARTED`、`SILENCE_CONTINUED`、`OPEN_THREAD_DUE`を`DO_NOTHING`にする。明示的な新規Directiveは抑止しない。
- 沈黙起点の発話上限を4回から2回/5分へ下げた。

## 検証

実行:

```powershell
$env:PYTHONPATH=(Resolve-Path '.venv\Lib\site-packages').Path
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m pytest tests/test_autonomy.py tests/test_autonomy_heartbeat.py tests/test_behavior_directive.py tests/test_directive_runtime.py tests/test_narration_session.py tests/test_request_flexibility.py -q
C:\Users\coala\AppData\Local\Programs\Python\Python311\python.exe -m compileall -q neuro_voice\dialogue neuro_voice\autonomy neuro_voice\pipeline.py neuro_voice\discord_bridge\bot.py
```

結果: 161 passed、compileall成功。

回帰には実ログの文言、「停止文から新規planを作らない」、pending-plan race、Local/Discord parity、停止後quietと期限後再評価を含む。

## 実機受入手順

1. GUIを再起動して「3分ラジオして」と依頼する。
2. 20〜30秒後に「よし、ラジオは一旦終わろう」と発話する。
3. 即時に無音になり、古い区間が後から流れないことを確認する。
4. 「いや、もうラジオは終わったよ」と言っても新しいラジオが始まらないことを確認する。
5. 5分間、silence/open-thread由来の独り言が再開しないことを確認する。
6. その間に明示的に「今度は1分だけ別の話をして」と頼めば開始できることを確認する。
7. Discordでも同じ手順を行う。

期待ログ:

- `BehaviorDirective user stop ... (plan/segment/generation/audio discarded)`
- `Autonomy speech suppressed reason=directive_user_stop duration=300.0s`
- quiet中のheartbeatは`quiet_after_stop:directive_user_stop`

## Rollback

停止語彙だけ戻す場合は`intent_plan.py`の`is_stop_request()`と`_STOP`を戻す。自律quietだけ戻す場合は各surfaceの`on_user_stop`と`autonomy.post_stop_quiet_seconds`を外す。ただしRuntimeのtask/audio一括取消は古い音声再出現防止に必要なので、個別に戻さないこと。
