# 認知カーネル Phase 4: 持続する内面状態

- 日時: 2026-08-01 22:10 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-01 22:10 の項
- 状態: **自動回帰済み・実機未検証**。機能フラグ5つとも既定 false

---

## 0. 先に読むところ

Phase 4 でいちばん重要なのは新機能ではなく、**監査で見つかった2件の死んだ配線**。

### (a) `irritation` は常に 0.0 だった

```
Mind.cognitive_state()  →  affect=self._temporal_self.snapshot()
TemporalSelf.snapshot() →  {..., "affect": {joy, warmth, calm, concern,
                                            sadness, frustration, ...}}
CognitiveState.irritation → affect_state.get("irritation", 0.0)
```

**階層が1つ違い、名前も違う**（`irritation` ではなく `frustration`）。
そのため `_affect_fit` の冗談の罰も、苛立ち時の抑制も、実装されてから一度も
効いていなかった。テストは `build_state(affect={"irritation": .8})` と
直接値を渡していたので通っていた——**実際の呼び出し側と違う形で検証していた**。

### (b) 関係性17軸のうち2軸しか届いていない

`RelationshipState` は trust / psychological_safety / respect / interest /
comfort / reciprocity / caution / unresolved_hurt / repair_willingness /
social_fatigue / irritation / familiarity / playfulness / emotional_closeness /
conversational_sync / uncertainty / tension を持ち、減衰も上限も実装済み。

行動選択が読めるのは `trust` と `comfort` だけ。残りは `style()` を経由して
**プロンプト文字列にしかならない**。

どちらも「機能はあるが繋がっていない」形で、動いているように見えるので
気づきにくい。Phase 4 の実体は、この2つを繋いだ上で規律を与えたこと。

## 1. Phase 3 の preflight で出た3件（先に修正済み）

| 症状 | 原因 | 修正 |
|---|---|---|
| 「もういいよ」で過去の失敗を引けない | `retrieval_trigger` が `unresolved_obligation` を先に見ていた | 終了の合図を先に見る順へ |
| 同上 | `_failure_patterns_for` が引き金の名前だけで決めていた | **状態から**決める（引き金＝なぜ探すか、パターン＝何を探すか） |
| 同じ失敗を繰り返しても Reflection が作られない | 新しい行ができた時だけ数えていた | `REINFORCE` も1回の観測として数える |

3件目がいちばん効く。**同じ失敗の繰り返しはいちばんよくある形**なのに、
2回目以降は `REINFORCE` になって新しい行ができないため、閾値へ永久に届かなかった。

## 2. 既存機能の分類

| 分類 | 対象 |
|---|---|
| **REUSE** | `RelationshipStore`（17軸・per-speaker・減衰・per-event/session 上限・repair イベント）、`TemporalSelf.AffectState`、`PersonalityEngine.TRAITS`、`trace.StateLimit` / `STATE_LIMITS`、`CognitiveState`、Phase 3 の `stance()` |
| **EXTEND** | `CognitiveState`（読み方の修正＋5軸）、`CognitiveKernel._score`、`CognitiveTrace`、`RelationshipStore.apply_state_delta` |
| **NEW** | `cognition/internal_state.py`、`mind/internal.py` |
| **DEAD_OR_DISCONNECTED** | 上の (a) (b)。加えて `RelationshipStore.apply_events` は内省LLM経由でしか呼ばれておらず、`ActionOutcome` から関係を動かす道が無かった |

**新しい感情モデルは作っていない。** `AffectState`（声の表情づけ）と
行動選択用の短期感情を1つに統合しなかったのは、目的が違うため——
声の抑揚を直したら行動選択が変わる状態にしたくない。対応づけは
`mind/internal.py` の `AFFECT_SOURCE_MAP` **1箇所だけ**。

## 3. 三層

| 層 | 持ち主 | 永続化 | 動かせるもの |
|---|---|---|---|
| 短期感情（数分） | `InternalStateService`（RAM） | **しない** | 出来事の意味評価 |
| セッションの構え | 同上（RAM） | しない | 短期感情から導出 |
| 長期の関係・好み | `RelationshipStore` / 記憶 | する | エピソードと Reflection のみ |

`StateDeltaGate` が層をまたぐ変更を落とす:

- `short_term_cannot_move_long_term` — **一瞬の苛立ちで信頼を削らない**
- `long_term_needs_grounded_source` — 推測だけでは長期を動かせない
- `long_term_needs_evidence_id` — 根拠IDが無い長期変化は残さない

短期感情の半減期は irritation 4分 / tension 5分 / caution 15分 /
confidence 30分。**苛立ちは早く冷め、警戒はもう少し残る**——意図的な非対称。

長期の関係値は時間だけでは戻さない。回復は良い経験・謝罪・訂正で起きるべきもの
（既存の `_decay` がすでにそう作られている）。

## 4. StateDelta の通り道

```
提案 → 型検証 → confidence ≥ .55 → 三層の整合 → 根拠IDの有無
     → 慣性（帯を2つ跨がない） → STATE_LIMITS の上限 → 重複 → 適用
```

慣性の帯は `[0,.25) neutral / [.25,.55) slight / [.55,1] high`。
**一度の出来事で neutral から high へは行けない**（信頼→不信、安心→敵対）。

重複は `(event_id, target, participant:dimension)` で見る。同じイベントを
二度適用しない。

`proposed_delta` と `applied_delta` を分けてあるのは、**どれだけ削られたか**を
読めるようにするため。合計だけ見ていると上限に張り付いていることに気づけない。

## 5. Action Selector への接続

`CognitiveKernel(state_bias=...)` → `_score` で `state_adjustment` として加算し、
`reasons` に `state:<軸名>` を残す。

| 状態 | 影響 |
|---|---|
| irritation 高 | `CONTINUE_PREVIOUS_TOPIC` −／`BRIEF_ACKNOWLEDGE` ＋／`MAKE_LIGHT_JOKE` − |
| caution 高 | `ANSWER` −／`ASK_CLARIFICATION` ＋／`CHALLENGE_ASSUMPTION` − |
| confidence 低 | `ANSWER` −／`ASK_CLARIFICATION` ＋ |
| curiosity 高 | `CONTINUE_PREVIOUS_TOPIC` ＋（**質問は増やさない**） |
| amusement＋playfulness＋comfort、かつ苛立ちも緊張も無い | `MAKE_LIGHT_JOKE` ＋ |

**±0.20 で頭打ち**（`MAX_STATE_BIAS`、記憶の補正と同じ大きさ）。

実装中に踏んだ罠: 足すたびに頭打ちにしていたため、苛立ちの −.24 が −.20 へ
丸められ、その後の面白さの +.20 で 0 になっていた。**合計してから一度だけ**
頭打ちにする。

## 6. 安全側 — ここが今回の主眼

```python
PROTECTED_ACTIONS = {WARN, REMAIN_SILENT, ABANDON_INTERRUPTED_RESPONSE}
```

`action_bias` はこの3つに**一切触れない**。理由:

- **`WARN`** — 嫌いな相手への警告を弱めるのは、いちばんやってはいけないこと
- **`REMAIN_SILENT`** — 沈黙は会話上の選択であって、不機嫌の表明でも罰でもない

加えて、敵意（`HOSTILITY`）に対しても `irritation` は上げず `caution` だけ
上げる。**腹を立てるのと距離を取るのは違う**——苛立ちで応じると、そこから先の
判断が全部それに引きずられる。

好みは二値化しない。`PreferenceState` は移動平均で、1件では `label` が
`unclear` のまま。4件そろって初めて `leans_positive` になり、
反対の証拠が来ても**符号はすぐには反転しない**。

テストで固定してあるもの:

- `test_emotion_never_touches_a_warning`
- `test_silence_is_never_an_emotional_punishment`
- `test_the_closure_path_still_wins_regardless_of_mood`
- `test_a_good_mood_cannot_unblock_a_low_confidence_answer`
- `test_one_good_experience_does_not_create_a_favourite`
- `test_one_bad_experience_does_not_flip_a_liking`
- `test_an_unknown_speaker_does_not_move_a_long_term_value`
- `test_one_participant_does_not_affect_another`

## 7. Planner への接続

`expression_constraints()` が返すのは**粗いラベルだけ**:

```yaml
affect:    {valence: slightly_positive, arousal: low, tension: low}
stance:    {mode: relaxed, playfulness: low}
relationship: {familiarity: medium, comfort: medium_high, caution: low}
expression_constraints:
  humor_allowed: true
  intensity: subtle
  verbosity: medium
  assertiveness: plain
```

生の数値も履歴も入らない（`test_the_planner_gets_coarse_labels_not_numbers`）。
`intensity` は控えめ側へ倒してある——内面が強いほど表現も強い、にすると
少しの苛立ちが語気に出る。

**ただし読む側は未実装。** `conversation_planner.py` へは繋いでいない（§11参照）。

## 8. テストとレイテンシ

```
tests/test_internal_state.py    51件
tests/test_episodic_memory.py   +3件（preflight の修正分）
全体                             1643 passed / 17 subtests
pyflakes                         clean
```

| 項目 | 実測 |
|---|---|
| `state_snapshot_ms` | 0.21 |
| `event_appraisal_ms` | <0.01 |
| `state_delta_ms` | 0.02 |
| `state_apply_ms` | 0.02 |
| `state_to_action_bias_ms` | 0.02 |
| `state_to_planner_ms` | 0.01 |
| **1ターン合計** | **0.23ms** |

**追加のLLM呼び出しは無い**（`test_a_turn_needs_no_extra_llm_call` で固定）。

## 9. 実機で確認すべきこと（この順で）

1. `internal_state.enabled: true` だけ。**行動は何も変わらないはず。**
   `cognition.trace.enabled: true` で `internal_state.summary` が出るのを確認
2. `affect_enabled: true`。説明を続けて遮られる → `internal_state.applied_deltas`
   に caution の小さな増加が出る → 数分後に戻る。
   **`clamp_reasons` が毎回同じ値で埋まっていたら閾値が厳しすぎる**
3. `planner_expression_enabled: true`。ただし**読む側が未実装なので今は効かない**
4. `preference_updates_enabled: true`
5. `relationship_updates_enabled: true` — **いちばん最後**。ここだけが
   元へ戻せない（`relationships_*.json` に残る）。上げる前に
   `cp data/relationships_poppo.json data/relationships_poppo.json.bak`

垂直スライスの確認手順:

```
1. ポッポに長めの説明をさせる
2. 途中で「もういいよ」と言って遮る（×3回）
3. trace の internal_state.summary.affect.caution が上がっているか
4. その後の似た場面で continue_previous_topic の state_adjustment が負か
5. 30分ほど別の話をしてから戻り、caution が基準値付近か
```

## 10. 残っている穴

- **実機未検証。** `cognition.enabled` 自体も実機で有効化したことがない
- **Planner の `internal_state` を読む側が未実装。** `planner_view()` は形を
  返すが `conversation_planner.py` へ繋いでいない。フラグを上げても効かない
- `PreferenceState` は RAM のみ。永続化していない
- **Discord 側は未着手**（Local のみ）。Phase 3 と合わせて第19条の借りが2つ
- 人格の三層分離（Core Persona / Learned Tendencies / Current Expression）は
  **設計として分けただけ**。Core Persona を Reflection や感情から守る
  コード上の防壁は無い。今は「そこへ書く経路が無い」だけで、
  禁止されているわけではない
- `AFFECT_SOURCE_MAP` は一方向（`TemporalSelf` → 短期感情）。
  逆方向が無いので、行動選択側の苛立ちは**声には出ない**
- 自分の失敗の検出はまだ2パターン（Phase 3 からの持ち越し）

## 11. rollback

`config/config.yaml` の `internal_state.*` を5つとも false（既定の状態）。
DBもファイルも新規に作らないので、フラグを戻せば元の経路に戻る。

**ただし `relationship_updates_enabled` を一度上げた後は、
`relationships_*.json` に書き込まれた値が残る。** そこだけはフラグを戻しても
消えないので、上げる前にバックアップを取ること。

コードごと戻す場合は新規2ファイルを消し、`state.py` の `irritation` を
元へ戻す——ただし**戻すと再び常に 0.0 になる**ことに注意。
