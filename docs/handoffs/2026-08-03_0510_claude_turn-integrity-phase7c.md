# Phase 7C: 重複発話・レイテンシ回帰・ペルソナ混入

- 日時: 2026-08-03 05:10 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-03 05:10 の項
- 状態: **partial**。計測基盤と特定できた原因は修正。**レイテンシの実測は実機待ち**
- 前段: `docs/handoffs/2026-08-03_0230_claude_tool-dialogue-phase7b.md`

---

## 0. 最初に断っておくこと

**3つのうち、原因を確定できたのは1つだけ**（ペルソナ混入）。
重複発話は「次に起きた時に層を特定できる」状態にしたが、
**実機で再現していないので原因は特定できていない**。レイテンシは
測れるようにしただけで、**内訳の数字はまだ無い**。

推測で3つ同時に直すと、直っていないのに直った気になる。そこは
やらなかった。

---

## 1. 重複発話の発生地点と原因

### 分けられるようにしたもの

```
LLM_GENERATION_DUPLICATE   生成された文章そのものが繰り返している
SPEECH_REQUEST_DUPLICATE   文章は1件なのに発話要求が2件
TTS_JOB_DUPLICATE          要求は1件なのにジョブが2件
PLAYBACK_DUPLICATE         ジョブは1件なのに音が2回
```

`TurnFrame.duplicate_site()` が**上流から順に**確定する。生成が既に
繰り返しているのに再生回数だけ見ると「再生の問題」に見えて、直す
場所が変わってしまう。

### 追えるようにした相関ID

```
turn_id / session_id
active_persona_id / active_persona_version / persona_epoch
input_event_id / action_decision_id / planner_request_id
llm_request_id / llm_response_id / speech_request_id
tts_job_id / playback_commit_id
memory_ids_used / reflection_ids_used / history_scope
tool_path_entered / tool_intent_reason
```

文章は6段階（LLM出力 → Surface Realizer後 → 分割後 → Speech Gate通過
→ TTS投入 → 再生開始）で記録する。**残すのは正規化した文章の
ハッシュ・文数・文字数・隣接文の最大類似度だけ。** 会話全文も
ペルソナプロンプト全文も通常ログへ書かない（テストで固定）。

### 原因について言えること

**まだ言えない。** 実機で再現していないので、どの層で増えたのかを
示す記録が無い。次に出た時に、🩺 と Trace の
`duplicate_site` を見れば1回で分かる。§10 の手順を用意した。

---

## 2. 重複防止の実装

**文章比較ではなく ID。**

```
1 ActionDecision
→ 0 か 1 の ConversationPlan
→ 0 か 1 の final SpeechRequest
→ 0 か 1 の active TTS job
→ 0 か 1 の completed playback
```

`TurnCommitLedger.claim()` は、同じ ID で2回目を取れない。加えて
**決定側からも数える**——再送で `speech_request_id` が作り直されると
ID だけでは止められないので、`decision_id` から出せる最終発話を
1件に制限してある。

ストリーミングの断片は複数でよいが、`claim_chunk(job_id, index)` で
**同じ断片を二度確定しない**。中断時は `release()` で戻せる。

### 最終防御（根本原因の代わりではない）

`collapse_adjacent_duplicates()` は**隣り合う、ほぼ同一の短い文**だけ
を1つにする。

やらないこと:

* 離れた文を意味の近さで消す
* 数字・否定・時制が違う文をまとめる
* 60字を超える長文を畳む
* **既に喋り終えた断片を書き換える**（`already_spoken` まで触らない）
* LLM を呼ぶ（テストで固定）

> **自分のテストが自分のバグを見つけた。** 強調を守る分岐を書いて
> テストも4件書いたが、**分岐を消しても全部緑のまま**だった。
> 「いや、いや、それは違う」は1文なので比較すらされず、
> 「少しずつ。少しずつ進めよう。」は文が違うので類似度で弾かれる。
> **関門を一度も通っていなかった。** 「いや。いや。」のような
> 完全に同じ強調を足したら、注入で赤くなるようになった。

---

## 3. 改修前後のレイテンシ

**測っていない。** 測れる状態にしただけ。

理由を正直に書く。実機の音声経路（マイク・Whisper・Ollama・SBV2）を
こちらの環境から動かせないので、**同じ入力・同じモデル・同じ端末**と
いう比較条件を満たせない。満たさずに出した数字は、比較の役に立たない
どころか誤った結論を作る。

用意したもの:

```
vad_end_to_asr_final_ms      turn_finalize_ms
state_snapshot_ms            memory_trigger_ms
memory_retrieval_ms          world_state_snapshot_ms
persona_context_build_ms     action_selection_ms
conversation_plan_ms         llm_queue_wait_ms
llm_first_token_ms           surface_realizer_ms
speech_gate_ms               tts_queue_wait_ms
tts_first_audio_ms
→ turn_end_to_first_audio_ms
```

* **待ち時間（`*_queue_wait_ms`）と実処理を分ける。** 混ぜると
  「LLMが遅い」で終わってしまう。
* **会話種別ごとに集計。** ツール会話は元々長いので、混ぜると
  通常会話の悪化が見えなくなる。
* **p50 / p90 / p95。** p95 だけ悪いのは「たまに重い処理が挟まる」
  ことで、直す場所が全く違う。
* `warmup` でモデルの初回読み込みを除外できる。
* `LatencyRecorder.compare(feature)` で、**その機能を入れると
  何ms増えるか**を出せる（A〜F の比較に使う）。

---

## 4. 遅延増加の内訳

**未計測。** ただし、コードを読んで**払っている場所**は特定した。

| 疑い | 根拠 | 状態 |
|---|---|---|
| 通常会話でもツール判定が走る | Phase 7B まで、フラグ off でも `PlanAdmissionGate` と `decide_permission()` までは通っていた | **塞いだ**（§5） |
| ペルソナ静的設定を毎ターン組む | `persona_context_build_ms` を測る枠は作ったが、キャッシュ実装は未着手 | **未着手** |
| 世界状態スナップショットの重複作成 | `world_state_snapshot_ms` の枠のみ | **未計測** |
| 記憶検索の件数とサイズ | `_scan_limit` はあるが、Planner へ渡す件数の上限は未確認 | **未計測** |

**数字が無い状態で「ここが原因」と書かない。** §10 の段階2で取る。

---

## 5. 通常会話から除外した不要処理

`detect_tool_intent()` を critical path の最初に置いた。**軽い語句
判定だけ**で、LLM も async も使わない（テストで固定。0.001ms）。

```
明確な Tool Intent なし
→ Tool Capability 検索なし
→ Plan Admission なし
→ Permission 判定なし
→ Confirmation 生成なし
→ ToolResult 用 LLM 処理なし
```

打ち消しを先に見る。**「調べてたの?」「調べなくていいよ」は依頼では
ない**——Phase 6 で一度踏んでいる。

`turn_integrity.skip_tool_path_without_intent` は**既定で有効**。
何もしないのが正しい既定なので、上げないと通常会話が払い続ける。

`tool_path_entered` を `TurnFrame` に残すので、**通常会話でここが
true になっていたら、それ自体が不具合**として見つかる。

---

## 6. ペルソナ情報のスコープ設計

```
GLOBAL_SYSTEM              ツール定義・安全規則
WORLD_SHARED               いま目の前の客観的な事実
EXPLICIT_SHARED            明示的に共有指定された事実
PERSONA_PRIVATE            固有設定・口調・知識・Reflection・失敗経験
PERSONA_USER_RELATIONSHIP  そのペルソナとユーザーの関係
SESSION_PERSONA            このセッション限りの方針・感情
```

**ペルソナを越えて読めるのは上の3つだけ**（テストで固定）。

`recommended_scope()` の既定は `PERSONA_PRIVATE`。**知らない種類は
私的側。** 「所有者不明だから共有」にしない——見えなさすぎるのは
「覚えていない」で済むが、逆は戻せない。

所有者もスコープも無い古い行は `legacy_unscoped` として**隔離**。
`allow_legacy_unscoped_memory: true` を明示した時だけ出る。

---

## 7. Memory／Reflection／Relationshipの分離

### すでに分かれていたもの

```
mind_<persona>.db              記憶の DB
relationships_<persona>.json   関係値
personality_ / dialogue_ / temporal_ / activities_ / game_session_
```

### 分かれていなかったもの（今回）

| | 状態 |
|---|---|
| **会話履歴** | **これが漏れの直接原因。** §8 |
| 検索時点のスコープ | `memories` へ `persona_id` / `scope` 列と SQL 条件を追加 |
| キャッシュ鍵 | `cache_key()` が `persona_id + version + epoch` を含む |
| 進行中の非同期結果 | `PersonaStamp` と `epoch` 照合 |
| `ToolRuntime` | 切替で破棄（確認待ちを持ち越さない） |

**検索の時点で絞る。** 取ってからプロンプトで外す形だと、`LIMIT` を
別人格の記憶が先に埋めて、**漏れてはいないのに自分の記憶が1件も
出てこない**状態になる。

> **ここでもう1つ踏んだ。** `_existing()` に新しい引数を無条件で
> 渡したら、古い呼び出し規約の Store が `TypeError` になり、それが
> `contextlib.suppress` に飲まれて**検索結果が静かに0件**になった
> （既存テスト4件が落ちて気づいた）。人格が結ばれていない時は
> 引数を足さない形へ直し、**古い Store でも0件にならないこと**を
> テストで固定した。「漏れてはいないが何も思い出さない」は、
> いちばん気づきにくい壊れ方。

---

## 8. Persona Switch時の失効処理

```
SWITCH_REQUESTED
→ drain()（走っているものを終わらせる／失効させる）
→ persona_epoch を +1        ← **ここから旧世代の結果は通らない**
→ 人格固有キャッシュを捨てる
→ SWITCH_COMPLETED
```

順序をテストで固定してある（`drain` が `invalidate` より先）。
**止める前に世代を進めると、その隙間の結果がどちらの持ち物か
決められない。**

### 会話履歴（実機で見た漏れの直接原因）

`webview_app` は `conv.set_system_prompt()` だけを呼んでいた。
system prompt は新しくなるのに、**`_history` に旧ペルソナの AI 発話が
残ったまま次のターンへ渡っていた**。モデルから見れば「自分が直前に
そう喋った」ので、口調も知識もそのまま続く。

`ConversationManager.switch_persona()` を足し、**履歴・保留話題・
中断発話を捨てる**。共有してよい情報は明示的な共有スコープの記憶から
引き直す——ここで「これは共通の話題だから残そう」と選り分けると、
その判断の根拠が旧ペルソナの文脈になってしまう。

### 世代の照合

```
result.persona_id    == active_persona_id
result.persona_epoch == active_persona_epoch
```

一致しなければ、状態更新・記憶保存・Planner投入・発話を拒否。
**切替中は何も受け入れない。**

`persona_id` だけでは足りない理由は A→B→A。戻ってきた時に
最初の A の結果が一致してしまう。テストで固定してある。

---

## 9. 追加テストと結果

- `tests/test_turn_integrity.py` — **80件**（仕様の17シナリオを全て含む）
- 全体 **2695 passed / 17 subtests**、pyflakes clean
- 診断 **要注意 0 件**（warm 44ms / 109プローブ。上限 250ms）

### 追加した9プローブ（層「ターン」）

```
turn.duplicate_site                重複を4層に分けられるか
turn.commit_once                   再配送で二重に出さないか
turn.keeps_intentional_repetition  意図した繰り返しを削っていないか
turn.tool_path_skipped             通常会話でツール処理を起動しないか
turn.latency_split                 待ちと実処理を分けて測れるか
persona.scope_isolation            別人格の記憶が候補へ入らないか
persona.search_time_filter         検索の時点で絞っているか
persona.switch_invalidates         切替前の結果とキャッシュが残らないか
persona.history_dropped            切替で会話履歴を捨てているか
```

### 故障注入

| 切ったもの | 赤くなったもの |
|---|---|
| 所有者不明を共有にする | テスト1件 + `persona.scope_isolation` |
| 世代を照合しない | テスト2件 + `persona.switch_invalidates` |
| 切替で履歴を残す | テスト1件 + `persona.history_dropped` |
| 強調も畳む | テスト4件 + `turn.keeps_intentional_repetition` |
| 再配送を通す | テスト3件 + `turn.commit_once` |
| 打ち消し語を見ない | テスト1件 + `turn.tool_path_skipped` |
| 取得後に絞る | テスト2件 + `persona.search_time_filter` |

### 自分のバグ2件

1. **強調を守る分岐が一度も通っていなかった**（§2）。分岐を消しても
   テストもプローブも緑。完全に同じ強調文を足して直した。
2. **新しい引数で検索が静かに0件になった**（§7）。`suppress` が
   `TypeError` を飲んでいた。

---

## 10. 実機確認手順

### 段階1: 計測を上げる（会話は変わらない）

1. `turn_integrity.turn_frame_enabled: true` /
   `latency_profiling_enabled: true`。
2. **応答が変わらないこと。** 記録が増えるだけ。
3. 🩺 の「ターン」層が全部緑。

### 段階2: レイテンシの内訳を取る

4. ウォームアップ3ターン → 計測30ターン。
   - 短文（「うん」「そうなんだ」）10回
   - 通常文 10回
   - 記憶を使う会話 10回
5. ストリーミング ON / OFF を記録しておく。
6. フラグを1つずつ動かして比べる。**同じ入力・同じモデル・同じ端末。**

| | 設定 |
|---|---|
| A | 認知ON / Phase 7 ツールOFF |
| B | 認知ON / Phase 7B ON |
| C | `memory.retrieval_enabled: false` |
| D | `memory.reflection_enabled: false` |
| E | 世界状態参照OFF |
| F | `tools.result_dialogue_enabled: false` |

7. 見るところ:
   - **p50 だけでなく p95。** 遅いターンが特定の工程に集中しているか
   - `llm_queue_wait_ms` と `llm_first_token_ms` のどちらが大きいか
   - `tool_path_entered` が通常会話で false のままか

### 段階3: 重複が出た時

8. 出たら**その場で**次を確認する（後からでは分からない）。

| 見るところ | 意味 |
|---|---|
| 画面上の生成文も重複していたか | true なら生成時点 |
| 音声だけ重複したか | true なら再生かジョブ |
| 割込みや再接続の直後だったか | true なら再配送 |
| 同じ `speech_request_id` だったか | true なら台帳の取りこぼし |

9. 🩺 の `duplicate_site` が上の4層のどれを言っているか。
10. 同じ短文を10〜20回試して、**再現条件**を絞る。

### 段階4: ペルソナ分離（**耳と目で確認**）

11. `turn_integrity.persona_scope_enabled: true`。
12. **ペルソナAだけへ人工的で固有な言葉**を教える
    （例: 「青い三角形をルミナと呼ぶ」）。
13. A で使えることを確認。
14. **B へ切り替える。**
15. 新しいセッションと**同一セッションの両方**で聞く。
16. 見るところ:
    - B が「ルミナ」を知っている前提で答えないこと
    - **出力だけで判断しない。** 🩺 で次を見る:
      - Planner へ渡された `memory_id`
      - その記憶の `persona_id`
      - prompt section の scope
      - cache key
      - `persona_epoch`
17. **A へ戻す。** A では保持されていること。
18. 実際の「うーん」も試す。**同じ反応が出ても、それだけで漏洩と
    断定しない**——一般的な返答は偶然一致する。使われた memory /
    history / prompt cache の `persona_id` を見る。

**段階3と段階4は、実際に見て聞くまで完了扱いにしない。**

---

## 11. 旧経路への戻し方

| 落とすフラグ | 戻る先 |
|---|---|
| `turn_integrity.persona_scope_enabled` | 記憶の絞り込みが効かなくなる（列は残る） |
| `turn_integrity.allow_legacy_unscoped_memory: true` | 所有者不明の古い記憶も出す（**漏れる側へ倒す**） |
| `turn_integrity.collapse_adjacent_duplicates` | 隣接重複を畳まない |
| `turn_integrity.latency_profiling_enabled` | 計測を止める |
| `turn_integrity.turn_frame_enabled` | 相関IDの記録を止める |
| `turn_integrity.skip_tool_path_without_intent: false` | **Phase 7B と同じ**——通常会話でもツール判定を払う |
| `turn_integrity.drop_history_on_persona_switch: false` | **切替で履歴を残す**（漏れが戻る） |

DB の列は追加のみ。落としても既存データは壊れない。

---

## 12. 残っている重要な穴

- **レイテンシの実測が無い。** 3000→4000-5000ms の内訳は
  推測のまま。§10 段階2 を実行するまで、原因を「説明できる」とは
  言えない。**完了条件を1つ満たしていない。**
- **重複発話の原因が未特定。** 再現していないので、4層のどこかも
  分かっていない。分類できる状態にしただけ。
- **`TurnFrame` / `TurnLatency` が実経路へ挿さっていない。** 型と
  集計はあるが、`pipeline` と `bot` の各段階で `note_text()` /
  `start()` / `stop()` を呼ぶ配線が未着手。**これが次の最大の仕事**
  ——挿さっていない計測は、無いのと同じ。
- **`TurnCommitLedger` も呼び出し側が無い。** 発話要求・TTS投入・
  再生完了の各所で `claim()` する配線が要る。
- **書き込み側の `persona_id` 伝播が未着手。** `MemoryCandidate` /
  `Reflection` / `Relationship` へ持たせていないので、**これから
  作られる記憶も `persona_id` が空**になる（＝隔離側に落ちる）。
  読み側だけ直した状態。
- **関係値の複合キー（`persona_id + person_id`）が未実装。**
  ファイルは分かれているので実害は小さいが、仕様が求めた形ではない。
- **ペルソナ静的設定のキャッシュが未実装。** `persona_context_build_ms`
  の枠だけある。

> **書きながら1つ拾った。** この節の下書きに
> 「`webview_app` が `switch_persona()` を呼んでいない」と書いた時点で、
> **それは残件ではなく未完成**だと気づいた。`ConversationManager` に
> 実装しても UI が `set_system_prompt()` を呼び続けていれば、
> 履歴は残ったまま——漏れは何も直っていない。Phase 6E で
> `attach_identity_runtime()` に呼び出し側が無かったのと同じ形。
> 繋いで、**UI から呼ばれていること**をテストで固定した。
