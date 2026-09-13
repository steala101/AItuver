# Phase 5 第一弾: 継続的注意

- 日時: 2026-08-02 01:15 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 01:15 の項
- 状態: 自動回帰済み。**実経路へは未接続**（意図的）

---

## 1. Preflight（7条件、すべて問題なし）

| 条件 | 結果 |
|---|---|
| 内面が Action Selector へ影響する | CONTINUE の点数 −0.2 |
| participant 単位の関係状態が分離される | A=.520 / B=.500 |
| 短期感情が減衰・回復する | .5 → .052（30分） |
| 記憶想起が必要時だけ動く | 挨拶=引き金なし / 参照=past_reference |
| Speech Gate が発話元と Action を検証する | 通常回答✗ / 警告✓ / 無根拠legacy✗ |
| 沈黙後の同一話題抑制 | 同話題=抑制 / 別話題=許可 |
| WARN 以外が高速経路を通らない | 同上 |

修正不要だった。

## 2. 既存自発発話機能の分類

| 分類 | 対象 |
|---|---|
| **REUSE** | `AutonomousActionSystem`（イベント・候補生成・`UtilityScorer`・heartbeat・`should_speak`）、`autonomy/types.py` の32イベント型、`ContinuationController`、`spontaneous_suppressed`、`_game_companion_loop` と抑制器、`note_directive_environment_event` |
| **EXTEND** | `CognitiveTrace`、診断プローブ |
| **NEW** | `cognition/attention.py` |
| **UNSAFE_BYPASS** | `respond_text(internal_event=True)` が `_cognitive_decide` を飛ばす |
| **DEAD_OR_DISCONNECTED** | 内面→Planner（Phase 4 から継続） |
| **DUPLICATE** | 無し |

`AutonomousActionSystem` は**作り直さない**。あれは「いつ動くか」を持っていて、
認知カーネルは「何をするか」を持っている。責務は重なっていない。

### UNSAFE_BYPASS の中身

```python
# pipeline.py:1568
if direct_reply is None and not internal_event:
    self._cognitive_decision = self._cognitive_decide(user_text, transcript)
    ...
    if not self._execute_or_stay_silent(response_id):
        return
```

`internal_event=True` の呼び出し元:

- `pipeline.py:3074` — ゲーム実況
- `pipeline.py:3216` — autonomy（`internal_event_kind="autonomy"`）

つまり**自発発話とゲーム実況は、沈黙の決定も記憶の補正も内面の補正も
掛からずに発話へ行く**。`_proactive_loop` の方は `respond_text(prompt)` を
素で呼んでいるので認知層を通る——**同じ「自発発話」でも経路が2つあり、
片方だけ通っている**状態。

## 3. 直していない理由

§2 は「WARN以外は通常の候補生成へ戻してください」と言っているが、
**この弾ではやらない。**

`_cognitive_decide(user_text, transcript)` は `user_text` を**ユーザー発話**として
扱う。ここに autonomy の内部プロンプトを渡すと、

- `_user_affect(user_text)` が内部プロンプトから相手の感情を推定する
- `retrieval_trigger` が内部プロンプトで記憶を引く
- `CognitiveState.ambiguity` が内部プロンプトで立つ

つまり**自分が自分に話しかけたことになる**。正しく通すには、
自発発話を「発話機会（opportunity）」として別の入口から入れる必要があり、
それは Phase 5 の次の弾（社会的コストと価値の評価 → Action Selector）の仕事。

いまは**管理者メニューに `DISCONNECTED` として出してある**。
🩺 → 注意 →「自発発話が認知層を通っているか」。

## 4. 作ったもの

### AttentionEvent（14種）

音声・映像・ゲーム・記憶・時間経過を1つの形へ。
`dialogue/events.py` も `autonomy/types.py` も**置き換えない**——
ここは同じ物差しで比べるための共通の形で、正本ではない。

### EventIntake（間引き）

**全フレーム・全ASR断片をイベントにしない**の実体。

| 落とす理由 | 条件 |
|---|---|
| `expired` | 期限切れ |
| `below_salience` | 重要度 < .15 |
| `low_confidence` | 確信度 < .3 |
| `duplicate` | 同じ `deduplication_key` が4秒以内 |

**ただし危険（tier 0）と直接質問（tier 1）は重要度でも重複でも落とさない。**
取りこぼす方が高くつく。

### ContinuousAttentionState

短い構造化状態だけ。**内部思考文も Chain of Thought も保存しない**——
保存すると次にそれをプロンプトへ入れたくなり、結局「考えた形跡」を
毎ターン積むことになる。

`apply_event()` は**注目対象を変えない**。向き先を決めるのは `FocusManager`。
取り込みと選択を同じ関数に混ぜると、イベントが来た順で注目が動く
（＝ヒステリシスが効かない）。

### FocusManager

**段が先、点数が後。**

```
0 危険
1 直接質問
2 ユーザー発話 / グループ発話 / 割り込まれた
3 期限のある約束
4 ゲームイベント / タスク進捗
5 関連する記憶
6 沈黙の閾値
7 環境変化
8 自分の発話開始 / 話題の終了
```

点数ではなく段にしているのは、危険が「たまたま点が高かった」で選ばれる形に
したくないため。どれだけ面白いゲームイベントが積み上がっても押しのけられない。

同じ段の中でだけヒステリシスが効く:

- `SWITCH_MARGIN = .05` — この差を超えないと乗り換えない
- `MIN_DWELL_SECONDS = 1.5` — 乗り換えた直後は動かさない
- **危険は両方を無視して割り込む**（`higher_tier`）

`select()` は状態を書き換えない。反映は `commit()`。分けてあるのは、
決めた理由だけログに残して適用しない、という使い方ができるように。

## 5. テストが見つけた自分の設計ミス

最初は**ヒステリシスを2つ**入れていた。

- 現状維持へ加点（`INCUMBENT_BONUS = .12`）
- 乗り換えに差を要求（`SWITCH_MARGIN = .15`）

`test_the_margin_branch_is_reachable` が落ちて発覚:
点数は重みの合計で割るので現実的な差は .0〜.2、加点 .12 がそれを飲み込み、
**差の判定が一度も通っていなかった**。

同じ目的の仕組みが2つあると、片方が死んでいることに気づけない。
**差の判定1つに統一**し、値を点数の実際の広がり（urgency 満点差で .21、
salience で .13）から決め直した。`test_hysteresis_is_one_mechanism_not_two` で
加点が戻ってこないよう固定してある。

あわせて `initiatives_within` が**未来の記録まで数えていた**のも修正。
`now - at <= seconds` だけだと、時計が巻き戻った時に未来の項目が全部
「直近」に見えて、連発の抑制が効かなくなる。

## 6. テスト

```
tests/test_attention.py   39件
全体                       1710 passed / 17 subtests
pyflakes                   clean
```

特に落としたくないもの:

- `test_danger_beats_everything_no_matter_the_score`
- `test_the_dwell_lock_never_holds_back_danger`
- `test_danger_is_never_dropped_for_being_quiet`
- `test_a_slightly_better_option_does_not_steal_the_focus`（4通り）
- `test_the_state_keeps_no_thinking_text`
- `test_applying_an_event_does_not_move_the_focus`

## 7. 次の弾でやること

1. **発話機会（opportunity）の評価** — 社会的コストと価値
2. `AttentionEvent` を作る側の配線（VAD・映像・ゲーム・沈黙タイマー・記憶）
3. **迂回の修正** — 自発発話を opportunity として Action Selector へ通す。
   `internal_event=True` の分岐を消すのではなく、**別の入口を作る**
4. `recent_initiative_history` を埋めて連発を抑える
5. 機能フラグと Trace

## 8. 残っている穴

- **実経路へ未接続。** `AttentionEvent` を作る側も `FocusDecision` を使う側も無い
- `pending_opportunities` は候補の入れ物であって、発話機会の評価はまだ無い
- Discord 側は未着手（Phase 3・4 と合わせて借りが3つ）
- 内面→Planner の `DISCONNECTED` は Phase 4 から未解消

## 9. rollback

`neuro_voice/cognition/attention.py` と `tests/test_attention.py` を消し、
`wiring.py` の「注意」層3プローブを外す。**実経路へ繋いでいないので、
消しても他は動く。**
