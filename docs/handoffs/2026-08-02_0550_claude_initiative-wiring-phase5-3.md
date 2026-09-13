# Phase 5 第三弾: 実経路への配線と迂回の修正

- 日時: 2026-08-02 05:50 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 05:50 の項
- 状態: 自動回帰済み。**実機未検証**。機能フラグ既定 false

---

## 1. 迂回の中身と、直し方

### 何が起きていたか

```python
# pipeline.py（修正前）
if direct_reply is None and not internal_event:
    self._cognitive_decision = self._cognitive_decide(user_text, transcript)
    ...
    if not self._execute_or_stay_silent(response_id):
        return
```

`internal_event=True` の呼び出し元は2つ:

- `_run_local_autonomous_turn` — 自発発話
- `_game_companion_loop` — ゲーム実況

つまり**自分から話す時だけ、沈黙の決定も記憶の補正も内面の補正も
掛からずに喋っていた。** いちばん抑制が要る経路が、いちばん素通しだった。

### なぜすぐ直せなかったか

素直に `_cognitive_decide(prompt)` を呼ぶと、内部プロンプトが
**ユーザー発話として**扱われる:

- `_user_affect(user_text)` が内部プロンプトから相手の感情を推定する
- `retrieval_trigger` が内部プロンプトで記憶を引く
- `CognitiveState.ambiguity` が内部プロンプトで立つ

**自分が自分に話しかけたことになる。**

### 直し方

**別の入口**を作った。

```
_proactive_decide(kind)
  → InitiativeRuntime.evaluate()        機会の一覧（沈黙を必ず含む）
  → CognitiveEvent(content="")          自分へ向けられていない出来事
  → kernel.propose(event, state, opportunities)
  → kernel.decide()
  → 選ばれたのが自発候補でなければ (None, None) を返す ＝ 黙る
```

渡すイベントは `SILENCE_TIMEOUT`（自発）か `VISUAL_CHANGE`（実況）。
どちらも `_UNADDRESSED_EVENTS` 側なので**答えるものが無い**扱いになる。
**内部プロンプトは Action Selector に一切触れない。**

決まった決定は `respond_text(proactive_decision=...)` で持ち込み、
`_execute_or_stay_silent` と ActionOutcome の経路に乗せる。

## 2. 誰が何を持つか

| もの | 持ち主 |
|---|---|
| 注意状態・間引き・注目対象・予算・警告の連打抑制 | `InitiativeRuntime`（`pipeline._initiative`、遅延生成） |
| いま話してよいかの材料 | `pipeline._speaking_conditions()` |
| 判定 | `initiative.suppression_reason()`（**1箇所だけ**） |
| 最終出口 | `rollout.check_speech()` + `runtime.confirm()` |

`InitiativeRuntime` を `pipeline` の外に置いたのは、Local と Discord で
同じ順序を2回書かないため。順序が2箇所にあると片方だけ直されて食い違う。

## 3. イベントの投入（3箇所）

| 場所 | イベント | dedup |
|---|---|---|
| `_on_speech_start` | `user_speech_started` | `user_speech` |
| `_on_game_event` | `game_event` | `game:<kind>` |
| 自発ループ | `silence_threshold` | `silence` |

**呼び出し側で「これは重要か」を判断しない。** 間引きと分類は
`EventIntake` と `classify_game_event` の仕事で、呼び出し側ごとに閾値を
持つと場所によって基準が変わる。

## 4. 機能フラグ

```yaml
initiative:
  enabled: false        # 1. 取り込み・機会づくり・トレースだけ
  speech_enabled: false # 2. 発話の判断が Action Selector へ移る
```

**`speech_enabled` は `cognition.enabled` が false だと効かない。**
認知層が止まっているのに自発発話だけ通すと、沈黙の決定も記憶の補正も
掛からない発話が復活する。管理者メニューのフラグ帯に依存を出してある。

## 5. 安全側

- `_proactive_decide` が例外で落ちたら **`(None, None)` を返して黙る。**
  自発発話は落ちた時に喋る方が危ない
- `InitiativeRuntime.evaluate` が落ちても空を返す（第17条）
- TTS投入直前に `runtime.confirm()`（再検証）と `check_speech`（Gate）の
  二重確認。**取り消した発話を後から再生しない**
- `record_spoken` の重さは返答の長さから決める（`1.0 + len/120`）——
  回数だけだと長い実況の連発が予算に引っかからない

## 6. 診断への反映

管理者メニュー（🩺）の「注意」層:

| プローブ | いまの状態 |
|---|---|
| 注目対象が一瞬の変化で振動しないか | OK |
| 同じ変化を何度も出来事にしないか | OK |
| 沈黙が常に候補にあるか | OK |
| 「一定秒数黙ったら話す」になっていないか | OK |
| 自発発話が Speech Gate で検証されるか | OK |
| **自発発話が認知層を通っているか** | **DISCONNECTED → OK** |
| 注意イベントが実際に流れているか | 起動中のみ（`counters` を読む） |
| 自発発話の判断が実際に切り替わるか | OFF（フラグ） |

`initiative.runtime` は**「配線したが一度も動いていない」を見分ける**ための
プローブ。実装があることは静的に確かめられるが、イベントが実際に届いて
いるかは数えないと分からない。有効なのに `events_seen == 0` なら
`DISCONNECTED` と出る。

## 7. テスト

```
tests/test_initiative_runtime.py   24件
全体                                1804 passed / 17 subtests
pyflakes                            clean
診断の全掃引                         5.2ms
```

実際の `pipeline` はマイクとGPUが要るので組み立てられない。
迂回が塞がっているかは**構造を読んで**確かめている:

- `_proactive_decide` / `proactive_speech_allowed` がある
- `respond_text` が `proactive_decision` を受け取る
- 受け取った決定が `_execute_or_stay_silent` に乗る
- 呼び出し元2箇所が新しい入口を使っている
- `note_attention_event` の投入が3箇所ある
- `_proactive_decide` が `cognition_active()` を見ている

### 既存テストを1件直した

`test_the_pipeline_gate_returns_early_for_silence` が
「最初の `_execute_or_stay_silent` の直後に return があるか」だけを見ていて、
私が書いたコメント中の言及に引っかかった。

**呼び出しの数だけ確かめる**形へ変えた。最初の1つだけ見ていると、
後から足された経路（まさに今回の自発発話）が打ち切らないまま通る。

## 8. 実機で確認すべきこと（この順で）

1. `initiative.enabled: true` だけ。**行動は何も変わらないはず。**
   🩺 →「注意イベントが実際に流れているか」が緑になり、
   `観測N件→採用M件` が増えるのを見る。
   **採用が0のままなら間引きが厳しすぎる**
2. `cognition.enabled: true` + `rollout_mode: test_session`
3. `initiative.speech_enabled: true`。**ここから挙動が変わる。**
   `logs/cognitive_trace.jsonl` の `initiative` を読み、
   `suppressed` の理由が妥当か確かめる
4. ゲームを回して、実況が減ったか（予算と分類が効いているか）を聴く

## 9. 残っている穴

- **実機未検証。** `initiative.enabled` を上げたことがない
- イベントの投入は3箇所のみ。**直接質問・未完了の約束・記憶の関連は未接続**
  （`DIRECT_QUESTION` / `OBLIGATION_DUE` / `MEMORY_RELEVANCE`）
- `relationship_fit` は `comfort - .5` の粗い近似。`timing_score` は既定値のまま
- **Discord 側は未着手。** Phase 3・4 と合わせて第19条の借りが3つ
- 内面→Planner の `DISCONNECTED` は Phase 4 から未解消
- `_proactive_decide` は毎回 `recall_for_action` と `internal_state` を引く。
  自発発話の頻度が上がると効いてくるので、実機で計測すること

## 10. rollback

`config/config.yaml` の `initiative.enabled` を false（既定）。
それだけで従来経路へ戻る。

コードごと戻す場合は `cognition/runtime.py` と
`tests/test_initiative_runtime.py` を消し、`pipeline.py` の
`_proactive_decide` / `proactive_speech_allowed` / `note_attention_event` /
`proactive_decision` と、呼び出し元2箇所を元へ戻す。

**ただし戻すと迂回が復活する**（自発発話が認知層を通らなくなる）。
診断の `attention.autonomy_bypass` が再び `DISCONNECTED` になるので、
戻したことには気づける。
