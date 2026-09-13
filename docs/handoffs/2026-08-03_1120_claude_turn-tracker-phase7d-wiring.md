# Phase 7D ②③: TurnFrame / TurnCommitLedger を実経路へ挿した

- 日時: 2026-08-03 11:20 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-03 11:20 の項
- 状態: **partial。Phase 7D は完了していない**（①実測、④⑤⑥⑦は未着手）
- 前段: `docs/handoffs/2026-08-03_0740_claude_turn-path-phase7d-partial.md`
- 入口: `docs/handoffs/_NEXT_SESSION.md`

---

## 0. 何を閉じたか

`_NEXT_SESSION.md` §2 の **②と③だけ**。①（実測）はチビの作業で、
④⑤⑥⑦には手を付けていない。

前回の handoff にこう書いてあった:

> `TurnCommitLedger` が実経路へ挿さっていないので、次に出ても
> `duplicate_stage` は記録されない。

そこを閉じた。**ただし既定は無効のまま**なので、次に重複が出た時に
層を知りたければ設定を1行変える必要がある（§5）。

---

## 1. 追加したもの

`neuro_voice/cognition/turn_tracker.py`（新規）

Phase 7C の型（`TurnFrame` / `TurnCommitLedger`）を実経路から使うための
薄い層。**Local と Discord が同じ物を使う。** 片側だけに置くと
「重複」という同じ言葉が経路ごとに違う意味になる（AGENTS.md）。

やっていること:

* ターン開始時に `TurnFrame` を1件作る。`TurnMetrics.turn_id` が正本。
  **冪等**——入口が1箇所に絞れない（マイク／打ち込み／自発発話／
  内部イベント）ので、2回目以降は最初の枠を返す。
* 確定（発話要求・TTSジョブ・再生）が二度来たら、**どの層かを
  `TurnMetrics.duplicate_stage` へ残す**。
* **既定では止めない。** 記録して素通しする。

やっていないこと:

* 文章の近さで重複を決めない。判定は ID の一意性だけ。
  「いや、いや」を消すのは `collapse_adjacent_duplicates` の担当。
* **ID が無い呼び出しを重複扱いにしない。** 素通しする。ここで止めると
  無関係な発話が消える。

---

## 2. 挿した場所

### Local (`neuro_voice/pipeline.py`)

| 地点 | 何をするか |
|---|---|
| `_respond()` / `_respond_from_text()` の `speech_end` 直後 | `_begin_turn()` |
| `respond_text()` の ID 確定直後 | `SPEECH_REQUEST` を `decision_id=utterance_id` で確定 |
| `_speak_loop()` の発話ゲート通過後 | `commit_chunk(response_id, 通し番号)` |
| `_on_play_start` callback | `PLAYBACK` を chunk_id で確定 |
| `_on_complete`（未完了時） | `PLAYBACK` の確定を**取り消す** |
| `_respond_direct_text()` | `TTS_JOB` と `PLAYBACK` を response_id で確定 |
| 生成完了時 / enforce 後 | `LLM_OUTPUT` / `SURFACE_REALIZED` を記録 |

### Discord (`neuro_voice/discord_bridge/bot.py`)

| 地点 | 何をするか |
|---|---|
| `_handle_utterance()` の `speech_end` 直後 | `_begin_turn()` |
| `_generate_and_speak()` の入口 | `SPEECH_REQUEST` を `decision_id=utterance_id` で確定 |
| `_speak()` の chunk 生成後 | `TTS_JOB` を chunk_id で確定 |
| `_speak()` の送出直前 | `PLAYBACK` を chunk_id で確定 |
| enforce の前後 | `LLM_OUTPUT` / `SURFACE_REALIZED` を記録 |

---

## 3. ついでに直した2つ（どちらも Phase 7D 前半の穴）

### `TurnMetrics.source_type` が Discord でも `local_mic` だった

前半で「経路別の合計。Local と Discord を混ぜない」を作ったのに、
**bot 側が `source_type` を一度も設定していなかった**。既定が
`local_mic` なので、Discord のターンが Local として集計されていた。
`LatencyWindow.totals()` が経路を分けているつもりで分けていない。

`_begin_turn()` で `SourceType.DISCORD_VOICE` を入れる。

### `metrics.bind_persona()` が実経路で一度も呼ばれていなかった

前半で `TurnMetrics` へ persona 3項目を足したが、**呼び出し側が無く**
🩺 のペルソナ欄は常に空だった。`_begin_turn()` で束縛する。

**この2つは `turn_frame_enabled` の外側でやる。** あれは Phase 7D 前半の
計測の背骨に属していて、今回の機能フラグの持ち物ではない。
（テスト `test_both_paths_bind_the_persona_outside_the_flag` が
フラグの内側へ移すと赤くなる。）

---

## 4. テストと結果

- `tests/test_turn_tracker_wiring.py`（新規, **27件**）
- `tests/test_turn_latency_wiring.py`: 固定長の窓を関数末尾までに変更
  （**順序を見たいのであって行数を見たいのではない**ので、数行足した
  だけで「見つからない」になるのは検査として弱い）
- 診断プローブ `turn.tracker_records_duplicate` を追加（**110件**）
- 全体 **2683 passed / 17 subtests**、対象ファイルの pyflakes clean
- 診断 **要注意 0 件**、warm sweep 約47ms / 110プローブ

### 故障注入（13種すべて赤くなることを確認）

| 戻したもの | 結果 |
|---|---|
| ターンの枠を毎回作り直す | テスト1件 + **プローブ1件** |
| 重複した層を後から上書きする | テスト1件 |
| 既定で発話を止める | テスト1件 + **プローブ1件** |
| ID が無いのを重複扱いにする | テスト1件 |
| Local が `speech_end` で枠を作らない | テスト1件 |
| Discord の `source_type` を上書きしない | テスト1件 |
| 断片番号を発話ループ間で通し番号にする | テスト1件 |
| 中断しても確定を取り消さない | テスト1件 |
| persona 束縛をフラグの内側へ動かす | テスト1件 |
| 段階記録を毎文足す | テスト1件 |
| 設定の既定を本番有効にする | テスト1件 |
| 即答が再生の前に `play_start` を打つ（7D前半の回帰） | テスト2件 |
| 枠を古い順に捨てない | テスト1件 |

### 故障注入で見つけた自分のバグ（1件）

**プローブの冪等性チェックが空振りしていた。**

```python
if tracker.begin(metrics) is not tracker.frame(metrics):   # ← 常に一致
```

枠を作り直す実装に戻しても、「2回目の戻り値」と「今入っている物」は
**どちらも新しい方**なので一致する。最初の枠を保持して比べる形へ直した。
同じ検査でも**テスト側は赤くなった**（`first is second` で比べていた）。
プローブだけを信じていたら通り抜けていた。

---

## 5. チビへ: 次に重複が出た時のために

**既定は無効のまま**（`turn_frame_enabled: false`）。前回の
「設定のデフォルトを勝手に本番有効へ変更しない」に従った。

次に「同じ文を2回言った」が起きた時に層を知りたければ、
`config/config.yaml` の1行だけ変える:

```yaml
turn_integrity:
  turn_frame_enabled: true          # ← ここだけ
  duplicate_suppression_enabled: false   # ← こちらは false のまま
```

* `turn_frame_enabled: true` — **記録するだけ。発話の挙動は変わらない。**
  重複が起きたターンの `duplicate_stage` に、4層のどれかが入る:

```
llm_generation_duplicate   生成された文章そのものが繰り返している
speech_request_duplicate   文章は1件なのに応答が2件立ち上がった
tts_job_duplicate          応答は1件なのに同じ断片が2回TTSへ来た
playback_duplicate         ジョブは1件なのに同じ音が2回鳴った
```

* `duplicate_suppression_enabled` は**まだ上げないでほしい。**
  原因が分からないうちに止めると、重複の代わりに欠落が出て、しかも
  今度は記録も残らない。層が分かってからで間に合う。

上げるかどうかはチビが決めること。こちらで既定は変えていない。

---

## 6. 残っている穴（正直に）

- **実測（①）は相変わらず無い。** レイテンシの内訳はまだ言えない。
  §6 の手順（前回 handoff）はそのまま有効。
- **重複の原因は特定できていない。** 特定できる**準備**が整っただけ。
  実機で再現して、上のフラグを上げて、層を見るまでは推測になる。
- **`TurnFrame.duplicate_site()` は実経路で使っていない。** あれは
  「1ターン＝1ジョブ」を前提に段数を数える形で、ストリーミングでは
  段数が正常に複数になるため、そのまま使うと毎ターン誤検出する。
  実経路の判定は台帳の拒否（ID の一意性）に寄せた。
- **Discord の `TTS_JOB` / `PLAYBACK` は chunk_id 鍵。** chunk_id は
  断片ごとに新しいので、捕まえられるのは「**同じ断片が再配送された**」形
  だけ。Local の `commit_chunk`（発話ループごとに 0 から数える）の方が
  「同じ応答に発話ループが2つ立った」を捕まえられる。Discord 側に
  同じ強さの検査を入れるには、送出の通し番号を `_speak()` へ渡す
  必要がある——15箇所の呼び出しを触ることになるので、**重複の層が
  実機で分かってから**にした方がよい。
- **④⑤⑥⑦は未着手。** 特に⑤（書き込み側の `persona_id`）は
  Phase 8 のブロッカーのまま。**新しく作られる記憶は今も
  `persona_id` が空**。
- **`docs/CODEMAP.md` を更新していない。** あれは snapshot 2026-07-30 で、
  `neuro_voice/cognition/` パッケージが**丸ごと載っていない**（Phase 6 以降
  の追加が全部漏れている）。今回の1ファイルだけ足すと、cognition の中で
  turn_tracker だけが地図にある状態になり、かえって誤読を招く。
  **cognition 全体を1回で足す作業として分けた方がよい**——`world.py`
  `goals.py` `identity.py` `presence.py` `dedup.py` `tools.py` `planning.py`
  `tool_exec.py` `tool_report.py` `tool_runtime.py` `turn_integrity.py`
  `turn_latency.py` `persona_scope.py` `rollout.py` `turn_tracker.py`。
  暫定の地図は `_NEXT_SESSION.md` §8 にある。

---

## 7. 戻し方

機能フラグを持つので、既定のまま（false）なら**この変更は何もしない**。
それでも戻したければ:

* `neuro_voice/cognition/turn_tracker.py` を削除
* `pipeline.py` / `bot.py` の `self._turns` と `_begin_turn` の呼び出しを外す
* `config/config.yaml` の `turn_frame_enabled` /
  `duplicate_suppression_enabled` / `frame_capacity` / `commit_capacity` を削除

**ただし §3 の2つ（Discord の `source_type`、persona の束縛）は戻さない
方がよい。** あれは機能ではなく、前半で作った計測が実際には効いて
いなかった箇所の修正で、戻すと 🩺 の経路別合計とペルソナ欄が
また嘘をつく。
