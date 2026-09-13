# Phase 7D（前半）: 実経路の計測終点を修正

- 日時: 2026-08-03 07:40 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-03 07:40 の項
- 状態: **partial。Phase 7D は完了していない**
- 監査: `docs/audits/2026-08-03_turn_path.md`
- 前段: `docs/handoffs/2026-08-03_0510_claude_turn-integrity-phase7c.md`

---

## 0. なぜ「完了」と書かないか

Phase 7D の完了条件は**実測**を要求している（30ターン以上、
p50/p90/p95、再生開始 callback を終点）。マイク・Whisper・Ollama・
SBV2 が要るので、こちらの環境からは実行できない。

仕様の第17項に「推測値を測定値として報告」「実経路へ未接続の計測を
実測と呼ぶ」が禁止されている。そのとおりにした。**数字は出していない。**

§10〜13（Memory/Reflection への `persona_id` 伝播、Relationship 複合
キー、静的キャッシュ）も**未着手**。中途半端に始めて壊すより、
手を付けていない状態で渡す方がよいと判断した。

---

## 1. 監査で分かったこと

**計測の背骨は既に実経路にあった。**

```
utils/latency.py の TurnMetrics
→ pipeline.py と bot.py が引数で運んでいる
→ LatencyWindow が p50/p95 を 🩺 へ出している
```

Phase 7C で作った `TurnLatency` は**これと二重**になる。新しい計測系を
もう1つ作るのではなく、既存を広げるのが正しい（第35項）。

そして、既存の背骨に**終点の誤りが2箇所**あった。

---

## 2. 直したこと

### 誤りその1: `pipeline.py` `_respond_direct_text()`

```
synthesize() が返る
→ mark("play_start")     ← **まだ鳴っていない**
→ playback.play(...)
```

音声生成の完了を再生開始として記録していた。制御応答とゲーム攻略の
即答がここを通る。

ストリーミング経路（`_speak_loop`）は callback を使っていて正しいので、
**同じ `play_start` という名前に、意味の違う2つが入っていた。**

`tts_audio_ready` を分け、`play_start` は `playback.play()` の**後**へ。

### 誤りその2: `bot.py` の Opus 送出

```
self._source.write(pcm)   ← **キューへ積むだけ**
→ mark("play_start")
→ vc.play(self._source)   ← 実際に鳴り始めるのはここ
```

`write()` は Opus ソースのキューへ積むだけ。**送出待ちの時間が合計から
丸ごと抜けていた。** `discord_enqueued` へ変え、`vc.play()` の後に
`play_start` を打つようにした。

DAVE 経路（サイドカー）も `discord_enqueued` を足した。あちらは
`await play_pcm()` の後なので元から Opus 側よりは近い。

### 影響

**🩺 に出る数字が、これまでより長く（正しく）出る。**
以前の数値と直接比較しないこと——比較すると「Phase 7D で遅くなった」
ように見えるが、**測り方が直っただけ**。

これは「4000〜5000ms へ悪化した」という報告の解釈にも効く。
**旧水準の約3000msが、この誤った終点で測られた値だった可能性がある。**
断定はしない（当時の計測条件が分からない）が、実測時は
`TTS生成` と `再生待ち` の区間を分けて見てほしい。

---

## 3. 計測の背骨に足したもの

```
turn_id / source_type / session_id / conversation_id / channel_id
persona_id / persona_version / persona_epoch / origin_turn_id
```

* **`not_applicable`。** 通らなかった段階を 0ms にしない。
  `skip()` を呼んだ段階と、mark が打たれなかった段階を区別する——
  混ぜると、記憶検索をしなかったターンと、記憶検索が壊れたターンが
  同じに見える。
* **p50 / p90 / p95 / min / max。**
* **経路別の合計。** Local と Discord を混ぜない。
* **Discord 固有の区間**（送出キュー・Discord再生）を、
  **同じ構造の任意フィールド**として追加。別の集計系は作っていない。
* percentile は **nearest-rank**。補間すると、30ターン程度の標本で
  **一度も起きていない値**が p95 になる。「95%のターンはこれより
  速かった」の根拠が実在しないのは困る。

---

## 4. テストと結果

- `tests/test_turn_latency_wiring.py` — **16件**
- 全体 **2712 passed / 17 subtests**、pyflakes clean

### 故障注入

| 戻したもの | 赤くなったもの |
|---|---|
| 生成完了を再生開始にする（pipeline） | 2件 |
| キューを再生開始にする（Discord） | 2件 |
| 通らなかった段階を 0ms 扱いにする | 3件 |
| percentile を補間へ戻す | 1件 |

`test_no_path_marks_playback_before_handing_audio_over` は
**全ての `play_start` を走査**して、音を渡す前に打たれていないことを
見る。1箇所直しても別の経路が同じことをしていたら意味がないので。

---

## 5. 次にやること（Phase 7D の残り）

順番に意味がある。**計測が正しくなった今が、実測の入口。**

1. **実測**（§7）。下の手順。
2. `TurnFrame` を発話確定時に1件作り、`TurnMetrics.turn_id` と
   同じ値にする。`speech_end` を打つ所（`pipeline.py:995` / `:1350`）が
   最初の地点。
3. `TurnCommitLedger` を発話経路へ挿す（§5）。
4. PersonaContext のターン固定（§9）。
5. Memory / Reflection への `persona_id` 伝播（§10・§11）。
6. Relationship 複合キー（§12）。
7. 静的キャッシュ（§13）——**`persona_context_build_ms` を測ってから。**
   有意でなければ作らない。

---

## 6. 実測手順（チビへ）

計測は既に動いている。フラグは要らない。

### 通常会話

1. ポッポを起動し、**ウォームアップに3ターン**話す（数字は捨てる）。
2. **同じ短文を30ターン。** 例:
   「おはよう」「そうなんだ」「どう思う?」「うん」「なるほど」を繰り返す。
3. 🩺 のレイテンシ表示を見る。出るのは:

```
STT / Context / Speaker / 記憶検索 / ペルソナ / LLM初回
TTS初回 / TTS生成 / 再生待ち / 合計
```

4. **`合計` の p50 / p90 / p95** を控える。
5. **`TTS生成` と `再生待ち` を分けて見る。** ここが今回直した所。
   以前は `再生待ち` が 0ms になっていた。

### 機能別の比較

同じ30ターンを、設定を1つずつ変えて繰り返す。
**同じ入力・同じモデル・同じ端末で。**

| | 変える設定 |
|---|---|
| A | そのまま（Phase 7B の機能 ON） |
| B | `tools.result_dialogue_enabled: false` |
| C | `memory.retrieval_enabled: false` |
| D | `memory.reflection_enabled: false` |
| E | 世界状態参照 OFF |

各回の `合計 p50` を並べれば、**どの機能が何ms分か**が出る。

### Discord

6. 可能なら Discord でも10ターン以上。🩺 で
   `送出キュー` と `Discord再生` が別々に出る。

### 数字が取れたら

そのまま貼ってくれれば、内訳の分析と、どこから直すかを一緒に決める。
**推測で先に直さない**——仕様の第8項どおり、計測してから最大の増加箇所へ。

---

## 7. 旧経路への戻し方

今回の変更は**計測の名前と位置だけ**で、機能フラグを持たない。
戻すなら:

* `pipeline.py` `_respond_direct_text()` の `mark("play_start")` を
  `playback.play()` の前へ戻す
* `bot.py` の `discord_enqueued` を `play_start` へ戻す

**ただし戻すと、再生開始までの時間が再び測れなくなる。**
数字が「良く」見えるだけで、実際の応答は何も変わらない。

`utils/latency.py` の追加項目（`turn_id` など）は既定値を持つので、
古い呼び出し方（`TurnMetrics({...})`）はそのまま動く。

---

## 8. 残っている重要な穴

- **実測が無い。** Phase 7D の完了条件を満たしていない。§6 を回すまで
  「4000〜5000ms の内訳」は言えない。
- **重複発話の原因は未特定のまま。** Phase 7C から進んでいない。
  `TurnCommitLedger` が実経路へ挿さっていないので、次に出ても
  `duplicate_stage` は記録されない。**ここは Phase 7C の報告より
  悪い状態ではないが、良くもなっていない。**
- **`TurnFrame` が実経路で作られていない。** `TurnMetrics` に
  `turn_id` を持たせたので受け皿はできたが、両者を繋いでいない。
- **§10〜13 が丸ごと未着手。** Memory/Reflection の `persona_id`、
  Relationship 複合キー、静的キャッシュ。Phase 7C で「読み側だけ
  直した」状態のまま。**新しく作られる記憶は今も `persona_id` が空**
  （＝隔離側に落ちる）。
- **Discord の `play_start` は「再生を開始した」であって「音が届いた」
  ではない。** ネットワーク越しの遅延はこの先にある。Local と同じ
  意味の数字ではないので、混ぜて比較しないこと。
