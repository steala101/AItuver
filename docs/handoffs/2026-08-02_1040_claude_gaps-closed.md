# 段階1の有効化と、残っていた穴を塞いだ記録

- 日時: 2026-08-02 10:40 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 10:40 の項
- 状態: 自動回帰済み。**実機未検証**

---

## 1. 有効にしたもの（段階1だけ）

```yaml
initiative:
  enabled: true          # ← ここだけ
  speech_enabled: false  # 発話の判断はまだ移さない
```

**行動は変わらない。** 注意イベントを取り込み、機会を作り、トレースへ
残すところまで。発話を決めるのは従来どおり `AutonomousActionSystem`。

`cognition.enabled` も `internal_state.*` も `memory.*` も false のまま。

## 2. 塞いだ5つの穴

### (1) `timing_score` が既定値のままだった

「価値はあるが今じゃない」を表現できていなかった。

| 状況 | 点数 |
|---|---|
| 相手が話し終えて0.8秒未満 | 0.15（食い気味） |
| 1〜2.5秒 | 0.85（いちばん自然） |
| 45秒以上空いた | 0.35（唐突） |
| 自分が直前に話した | 掛け算で下がる |
| 相手か自分が話している最中 | 0.0 |

計算するだけでは足りないので、`opportunities_from_events(timing=...)` で
機会へ配り、`InitiativeRuntime.evaluate` が実際の経過時間から求める。

### (2) 実人数を数えていなかった

`is_group` の2値だと3人と5人の差が出ない。
`Mind.participant_count()` が聞き手の一覧から数える（分からなければ
group なら2、そうでなければ1）。

### (3) `target_scope` がほぼ `GROUP` 固定だった

全部同じだと、**独り言が呼びかけと同じ重さ**で扱われる。

```
resume_obligation / remind / light_follow_up / share_memory → DIRECT
comment_on_game / react_to_event                            → GROUP
acknowledge_change                                          → AMBIENT
```

**相手が特定できていなければ `DIRECT` にしない**（`scope_for`）。
不明話者を特定人物として扱わないの実体がここ。

### (4) 内面が Planner の口調へ届いていなかった（Phase 4 からの黄色）

`expression_constraints()` は形を返していたが、読む側が無かった。

- `ConversationPlan.internal_state` を追加
- `_internal_state_line()` が**ラベルから一行の指示文**を作る
- `ConversationPlanner.prompt()` が呼ぶ
- `Mind.build_context` の**両方の分岐**から渡す（片方だけだと
  `include_recall` の有無で挙動が変わる）

**生の数値は渡さない。** 渡すとモデルが数字を根拠に語り始める
（「今の私の苛立ちは0.4なので」）。中身も作らせない——効かせるのは
口調・返答量・断定の強さ・冗談の可否だけ。

出力例:

```
いまの構え=少し身構えている。語気を強めず、短く落ち着いて返す、返答は短く、断定を避け…
```

内面が空なら**何も足さない**（常時汚染しない）。

### (5) Discord parity（第19条の借り3つ）

**手順が2箇所にあると、必ず片方だけ直されて食い違う。**
後始末を `Mind.close_turn()` の1箇所へ集めた:

```
record_turn（会話ログ・人格・関係性・経験の保存）
  → observe_internal_outcome（内面。結果が確定してから）
  → self_failure_pattern → record_self_failure
```

Local も Discord も**同じ1行**を呼ぶ。失敗パターンの判定も
`self_failure_pattern()` の1箇所（以前は pipeline のプライベートメソッド）。

Discord 側に置いたもの:

- `initiative()` — 自分の `InitiativeRuntime(source="discord")`
- `note_attention_event()` — 沈黙イベントの投入
- `_proactive_gate()` — 自発発話の抑制。**落ちたら黙る**
- `_speaking_conditions()` — 通話の材料（実在APIに合わせてある）

## 3. 診断

```
{ok: 26, off: 10, blocked: 2}   要注意 0 件   全掃引 9.5ms
```

**赤も黄色も残っていない。** 新しい層とプローブ:

| 層 | プローブ |
|---|---|
| Discord | ターンの後始末が Local と同じか / 自発発話の抑制があるか |
| 注意 | いま言うのが良いタイミングかを見ているか / 発話の宛先を振り分けているか |

`internal.planner` は**指示文になるところまで**見る形へ変えた。
形が作れるだけでは「読む側がある」ことにならないので。

## 4. テスト

```
tests/test_phase5_gaps_closed.py   32件
全体                                1888 passed / 17 subtests
```

`test_no_probe_is_broken_or_disconnected` が**要注意ゼロを固定**している。
新しい穴が開いたらここが落ちる。

## 5. 実機で確認すべきこと

1. 起動して 🩺 →「注意イベントが実際に流れているか」。
   `観測N件→採用M件` が増えるのを見る。
   **採用が0のままなら間引きが厳しすぎる**（`initiative.min_salience`）
2. 会話を数ターンして、応答が今までどおりかを確認。
   **段階1では何も変わらないはず。** 変わっていたら何かが漏れている
3. `cognition.trace.enabled: true` にして `logs/cognitive_trace.jsonl` の
   `initiative` を読む。`suppressed` の理由が妥当か
4. そのあと段階2（`cognition.enabled` → `initiative.speech_enabled`）

## 6. 残っている穴

- **実機未検証。** これがいちばん大きい
- **Discord の `_proactive_gate` は抑制だけ。** Local のように
  Action Selector で候補を比べる形にはなっていない。`speech_enabled` を
  上げた時、Local は「沈黙が候補に並んで比較される」が、Discord は
  「抑制条件に引っかからなければ従来どおり喋る」——**意味論が完全には
  揃っていない**
- **Discord の通常応答は `_cognitive_decide` を通っていない。**
  記憶・内面・Planner は共通だが、行動選択は Local だけ
- `timing_score` の閾値（0.8秒 / 2.5秒 / 45秒）は**実測ではなく推定**。
  実機で聴いて調整すること
- `participant_count` は `audience` が渡っている時だけ正確。
  Local では常に1になる
