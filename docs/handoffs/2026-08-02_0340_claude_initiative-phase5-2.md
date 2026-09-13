# Phase 5 第二弾: 発話機会・予算・Speech Gate

- 日時: 2026-08-02 03:40 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 03:40 の項
- 状態: 自動回帰済み。**実経路へは未接続**（第一弾と同じ理由）

---

## 1. 通したもの

```
注意イベント → 機会の候補 → 価値とコストの評価
            → Action Selector（通常会話と同じ列）
            → Speech Gate → 発話直前の再検証 → 発話または沈黙
```

**候補を作った時点では話さない。** 必ず Action Selector を通る。

## 2. 守った4つ

### 自発発話専用の Action Selector を作らない

別系統にすると、通常会話と自発発話のどちらを優先するかを決める場所が
もう1つ増えて、必ず食い違う。`CognitiveKernel.propose(..., opportunities)` で
同じ列へ並べる。機会にも記憶と内面の補正が同じように掛かる——
**自発発話だけ補正を免れると、過去に嫌がられた話題を自分からは平気で持ち出す。**

### 沈黙を常に候補へ入れる

`_with_opportunities()` が、候補に `REMAIN_SILENT` が無ければ必ず足す。
機会があることと話すべきことは別。ここを外すと「機会を見つけた＝話す」になり、
結局うるさいAIになる。

### 時間は発話の理由にならない

`opportunities_from_events` は `SILENCE_THRESHOLD` を、**未完了の約束がある時
にだけ**機会にする。「一定秒数黙ったら必ず話す」は作らない。
静かにしていたいだけの時に話しかけてくるAIになる。

### 危険は自発発話の経路に載せない

`PROACTIVE_ALLOWLIST` に `WARN` も `ANSWER` も入れていない。
危険を予算と社会的コストで扱うのは優先順位を間違えている。
`GAME_POLICY[DANGER] = None`（＝機会を作らない）で、危険は WARN 高速経路の仕事。

## 3. 発話してはいけない状態（`suppression_reason`）

判定は**1箇所だけ**。散らばると「なぜ黙ったのか」が説明できなくなる。

```
opportunity_expired / confidence_too_low
user_is_speaking / another_participant_is_speaking / awaiting_end_of_turn
already_speaking / handling_interruption
just_closed_the_turn / same_topic_after_silence / topic_closed
user_is_focused / only_unknown_speakers
event_already_handled / already_said_this
（予算の理由）
```

**沈黙だけは、どんな状態でも止まらない**（`REMAIN_SILENT` は即 `""` を返す）。

## 4. 予算

```python
InitiativeBudget(window_seconds=120, max_utterances=4,
                 max_same_topic_utterances=2, cooldown_seconds=8, max_cost=3.0)
```

**回数だけでなく重さも数える。** 短い相槌10回と長い解説1回では相手にとっての
重さが違う。回数だけだと、長い実況を連発しても引っかからない。

警告は予算の対象外だが、`WarnThrottle` が**同じ警告の連打**（6秒）を抑える。
別の警告は止めない。

## 5. ゲームイベントの分類

**画面認識の結果をそのまま文章にしない。** まず8種へ分け、分類ごとに方針を持つ。

| 分類 | 方針 |
|---|---|
| DANGER | 機会を作らない（WARN 高速経路） |
| MAJOR_PROGRESS | 実況候補（価値 .80） |
| UNEXPECTED_EVENT | 反応候補（.70） |
| FAILURE / SUCCESS | 短い反応候補（.60） |
| DISCOVERY | **novelty ≥ .65 の時だけ**（.55） |
| REPETITIVE_EVENT | 沈黙 |
| AMBIENT_CHANGE | 沈黙 |

`repeat_count >= 2` を先に見るので、**同じことが何度も起きているなら
それが何であれ実況の価値が落ちる**（初回だけ反応）。

## 6. 統合（`merge_opportunities`）

「敵が出た」「HPが減った」「避けた」を3回別々に喋らない。
同じ話題の近い時間（6秒）の機会を1つにまとめる。

**いちばん新しいものを土台にする。** 古い方を土台にすると
「さっき敵が出た」で止まって「もう避けた」が落ちる。状況は最新が正しい。

## 7. Speech Gate（`PROACTIVE_OPPORTUNITY`）

| 検証 | 拒否理由 |
|---|---|
| 決定がある | `proactive_speech_without_decision` |
| 決定が発話を許す | `decision_does_not_speak` |
| 行動が allowlist にある | `proactive_action_not_allowlisted` |
| 期限内 | `opportunity_expired` |
| 相手が話していない | `user_is_speaking` |
| Closure 抑制なし | `closure:<理由>` |
| 確信度 ≥ .55 | `proactive_confidence_too_low` |

**発話元不明の自発TTS要求は通らない。** 逆向き（`COMMENT` が警告の経路から
出る）も塞いである。

自分から話す確信度の下限（.55）を、危険警告（.60）と別に持っているのは、
**こちらから話しかけて外すのは、聞かれて外すより気まずい**ため。

## 8. 発話直前の再検証（`revalidate`）

候補を選んでから音が出るまでに状況は変わる。

```
expired_before_speaking / user_started_speaking
another_participant_started_speaking / higher_priority_event_arrived
topic_closed_meanwhile / already_spoken_by_another_path
event_handled_by_another_path
```

**取り消した発話を後から突然再生しない。** ここで捨てきる。

## 9. 実装中に見つけた2件

### (a) 2つの点数系が別の目盛りだった

`initiative_score` は読みやすさのため −1〜1 に正規化してあったが、
`CognitiveKernel._score` の総点は 0.8〜1.6 に出る。**同じ列へ並べても
自発発話の候補が一度も勝てなかった**（強い機会 0.40 < ANSWER 1.29）。

`INITIATIVE_BASELINE = .85` を足して目盛りを合わせた。値は
**沈黙の既定点（約1.09）のすぐ下**に置いてある——つまり
価値がコストを上回った機会だけが沈黙を越えられる。

実測:

| 機会 | 正規化 | 候補の点 | 沈黙(1.09) に勝つか |
|---|---|---|---|
| 弱い（コスト高） | −0.036 | 0.81 | ✗ |
| 強い（価値高） | +0.403 | 1.25 | ✓ |
| 全コスト最大 | −0.333 | 0.52 | ✗ |

### (b) 画面が変わっただけで `ANSWER` が最有力になっていた

誰も何も聞いていないのに。`propose` の既定分岐が
`_conversation_candidates`（＝`ANSWER` を goal_relevance .8 で提案）だったため。

`_UNADDRESSED_EVENTS`（VISUAL_CHANGE / TOOL_RESULT / ACTION_COMPLETED /
ACTION_FAILED / SESSION_STARTED / SESSION_ENDED）では
**「答えるものが無い」**として沈黙＋機会だけを出すようにした。

## 10. テスト

```
tests/test_initiative.py   70件
全体                        1780 passed / 17 subtests
pyflakes                    clean
診断の全掃引                 4.9ms
```

特に落としたくないもの:

- `test_silence_alone_never_creates_an_opportunity`
- `test_silence_is_always_in_the_candidate_list`
- `test_a_danger_event_is_not_displaced_by_an_opportunity`
- `test_a_warning_does_not_leave_through_the_proactive_path`
- `test_proactive_speech_without_a_decision_is_refused`
- `test_memory_penalties_apply_to_proactive_candidates_too`
- `test_the_newest_event_wins_when_merging`
- `test_a_candidate_is_cancelled_if_the_user_starts_speaking`

## 11. 次の弾（第三弾）でやること

1. **実経路への配線** — `AttentionEvent` を作る側（VAD・映像・ゲーム・沈黙タイマー・記憶）
2. **迂回の修正** — 自発発話を機会として Action Selector へ通す。
   `internal_event=True` の分岐を消すのではなく、**別の入口を作る**
3. `InitiativeBudget` / `WarnThrottle` / `ContinuousAttentionState` を
   `Mind` か `pipeline` のどちらが持つかを決めて配線
4. 機能フラグ（`initiative.enabled` など、既定 false）
5. Trace とレイテンシ計測

## 12. 残っている穴

- **実経路へ未接続。** 機会を Action Selector へ渡す呼び出し元が無い
- **自発発話の迂回は未修正。** 管理者メニューに `DISCONNECTED` で出したまま
- 機能フラグ未実装
- `InitiativeBudget` / `WarnThrottle` を持ち回る場所が無い
- `relationship_fit` / `timing_score` を実際に計算する側が無い（引数で受けるだけ）
- Discord 側は未着手（借りが3つ）

## 13. rollback

`neuro_voice/cognition/initiative.py` と `tests/test_initiative.py` を消し、
`types.py` の6つの `ActionType` と出どころのフィールド、`kernel.py` の
`_with_opportunities` / `_UNADDRESSED_EVENTS`、`rollout.py` の
`PROACTIVE_OPPORTUNITY` を戻す。

ただし **`_UNADDRESSED_EVENTS` の修正は残すこと**——
あれは自発発話とは独立した実物の欠陥（画面が変わっただけで `ANSWER`）。
