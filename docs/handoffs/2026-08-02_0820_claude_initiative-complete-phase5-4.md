# Phase 5 完結: 内面・記憶・複数人・段階フラグ

- 日時: 2026-08-02 08:20 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 08:20 の項
- 状態: 自動回帰済み。**実機未検証**。機能フラグ既定 false

---

## 1. 内面との接続（§16）

**強制命令ではなく小さな補正。** 「好奇心が高いから喋る」ではなく
「好奇心が高いから、発見への一言が少し通りやすい」。

| 内面 | 効き方 |
|---|---|
| curiosity 高 | **発見**への一言だけ＋。質問は増やさない |
| playfulness 高＋comfort 高 | **安全な成功**への軽口だけ＋。失敗には効かせない |
| comfort 高 | 軽い反応へ小さく＋ |
| irritation 高 | **コストを上げるだけ。** 増やす方向へは一切効かせない |
| caution 高 | 確信の低い画面認識へのコストを上げる |

上限 ±0.12。行動側の補正（±0.20）より小さくしてあるのは、
**二重に効く**ため。

`apply_internal_state` が触るのは価値とコストだけで、**行動そのものは
変えない**。「苛立っているから攻撃的な発話を選ぶ」経路を作らない。

## 2. 記憶との接続（§17）

**記憶をそのまま突然話さない。**

```
取得 → Initiative Opportunity → Action Selector → Speech Gate
```

使うのは4種だけ（`MEMORY_INITIATIVE`）:

| 記憶 | 機会 | 価値 |
|---|---|---|
| `promise` | RESUME_OBLIGATION | .85 |
| `preference` | LIGHT_FOLLOW_UP | .45 |
| `correction` | LIGHT_FOLLOW_UP | .45 |
| `strong_affect` | SHARE_RELEVANT_MEMORY | .50 |

**過去の失敗（`self_failure:*`）は候補にしない。** あれは「思い出して話す」
ためではなく「**同じ手を打たない**」ため。Action Selector 側の減点として
既に効いている。

関連度の下限 `.35` が、**古い記憶を脈絡なく披露するのを防ぐ実体**。
`source_memory_ids` を必ず持たせてあるので、後から由来を辿れる。

## 3. 複数人会話（§18）

```python
target_participant_ids: tuple[str, ...]
target_scope: DIRECT | GROUP | AMBIENT
```

**人が増えるほど割り込みは高くつく。**

| 場面 | コスト |
|---|---|
| 1対1 | 0.0 |
| 3人 | 0.38 |
| 3人＋他人同士が会話中 | 0.83 |
| 名指しで聞かれている | **0.0** |

最後の行が要る。名指しで聞かれているのに「複数人だから」で黙るのは
おかしい。

`AMBIENT`（誰に向けるでもない独り言）がいちばん高い——複数人の場で
いちばん邪魔になるのがそれなので。

不明話者だけの場は `only_unknown_speakers` で抑制される（第三弾から）。

## 4. 段階フラグ（§23）

```yaml
initiative:
  enabled: false                       # 全体
  speech_enabled: false                # 発話の判断が移る
  attention_enabled: true              # 取り込み
  game_commentary_enabled: false       # 実況の候補
  memory_initiative_enabled: false     # 記憶起点の候補
  idle_initiative_enabled: false       # 沈黙中の再開
  pre_speech_revalidation_enabled: true
```

`_KIND_FLAGS` が機会の種類ごとにどのフラグを見るかを持っている。
段階を上げていない種類は `stage_disabled` として落ちる。

**`pre_speech_revalidation_enabled` だけ既定 true。** 止める理由は
普通は無い——止めると「相手が話し始めたのに喋り出す」が復活する。

## 5. シナリオテストが見つけた実物のバグ

`test_case2_a_meaningful_game_event_reaches_a_decision` が
「決定の出どころが自発発話になっていない」で落ちた。

原因: `_with_opportunities` の期限判定が `candidate.expired()` を
**引数なし**で呼んでいて、`time.monotonic()` を直接読んでいた。
テストが注入した時計（1001.0）で作られた機会は、実際の monotonic
（約40000秒）から見れば全部「期限切れ」。**候補が静かに全部消えていた。**

実機では偶然動く（同じ時計なので）。**試験も再現もできない形**だった
のが問題。`propose` / `decide` / `_with_opportunities` へ `now` を通した。

`test_case2_the_expiry_check_uses_the_callers_clock` で固定してある。

## 6. 必須12シナリオ

```
tests/test_initiative_scenarios.py   52件
```

| ケース | 何を固定したか |
|---|---|
| 1 無意味な沈黙 | 時間だけでは発話しない／約束があれば別 |
| 2 有意味なゲームイベント | 一往復通る／沈黙と比較される／段階フラグで止まる |
| 3 反復イベント | 6件→1件／関連は統合／話した後は繰り返さない |
| 4 候補後のユーザー発話 | 再検証と Gate の**二重**で止める |
| 5 Turn Closure 直後 | 同一話題を再開しない／直後の静かな窓 |
| 6 別話題の重要イベント | **Closure を全イベントへ適用しない**／危険は WARN |
| 7 WARN 高速経路 | 予算に阻害されない／同じ警告は抑制 |
| 8 WARN 以外 | 5種すべて拒否 |
| 9 発話予算 | 8候補→3件以下／cooldown |
| 10 複数人 | 他人同士の会話は高コスト／名指しは無料／不明話者だけなら黙る |
| 11 未完了の約束 | 候補になる／関連が薄ければならない／失敗は候補にしない |
| 12 期限切れ | 再検証・Gate・取り込みの3箇所で落ちる |

加えて内面（6件）・レイテンシ・トレースの秘匿を固定。

## 7. レイテンシ（12イベントを溜めた状態）

| 項目 | 実測 |
|---|---|
| `focus_ms` | 0.13 |
| `opportunity_generation_ms` | 0.32 |
| `deduplication_ms` | 0.03 |
| `initiative_scoring_ms` | 0.005 |
| `suppression_ms` | 0.05 |
| `pre_speech_revalidation_ms` | 0.003 |
| **1回の評価** | **0.47ms** |

**通常イベントごとのLLM呼び出しは無し**（`test_no_llm_is_needed_for_an_ordinary_event`）。

診断の全掃引 4.8ms。

## 8. 診断（🩺 → 注意）

```
ok 21 / off 10 / blocked 2 / disconnected 1
```

「注意」層は11プローブ:

- 注目対象が振動しないか / 同じ変化を何度も出来事にしないか
- 沈黙が常に候補にあるか / 時間が理由になっていないか
- Speech Gate / 認知層を通っているか
- **記憶をそのまま喋らないか**（新）
- **内面が実況量を暴走させないか**（新）
- **複数人の会話を邪魔しないか**（新）
- イベントが実際に流れているか / 発話の判断が切り替わるか

**残る黄色は1つだけ**——Phase 4 の「内面が Planner の表現へ渡るか」。

## 9. 実機で確認すべきこと（この順で）

1. `initiative.enabled: true` のみ。**行動は変わらない。**
   🩺 の「注意イベントが実際に流れているか」で `観測N件→採用M件` を見る。
   **採用が0のままなら間引きが厳しすぎる**
2. `cognition.enabled: true` + `rollout_mode: test_session`
3. `initiative.speech_enabled: true` + `game_commentary_enabled: true`。
   **ここから挙動が変わる。** ゲームを回して実況が減ったかを聴く。
   `logs/cognitive_trace.jsonl` の `initiative.suppressed` の理由が
   妥当かを読む
4. `memory_initiative_enabled: true`。**約束の再開が自然かどうか**を見る。
   不自然なら関連度の下限（`MEMORY_RELEVANCE_FLOOR`）を上げる
5. `idle_initiative_enabled: true`

## 10. 残っている穴

- **実機未検証。** `initiative.enabled` を上げたことがない
- `timing_score` を実際に計算する側が無い（既定 .5 のまま）。
  「いま言うのが良いタイミングか」は現状ほぼ効いていない
- `participant_count` は `state.is_group` からの2値近似。**実人数を
  数えていない**ので、3人と5人の差が出ない
- `target_scope` は機会づくり側でほぼ `GROUP` 固定。DIRECT/AMBIENT を
  実際に振り分ける経路が無い
- **Discord 側は未着手。** Phase 3・4・5 で第19条の借りが3つ
- 内面→Planner の `DISCONNECTED`（Phase 4 から）

## 11. rollback

`config/config.yaml` の `initiative.enabled` を false（既定）。
段階ごとに1つずつ戻すこともできる。

コードごと戻す場合、**`kernel.py` の `now` の追加は残すこと**——
あれは自発発話とは独立した実物の欠陥（期限判定が別の時計を読んでいた）。
