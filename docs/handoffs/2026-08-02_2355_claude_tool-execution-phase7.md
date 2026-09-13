# Phase 7: 目標から、安全に道具を使うまで

- 日時: 2026-08-02 23:55 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 23:55 の項
- 状態: 自動回帰済み。**実機未検証**（そのため全フラグ false）
- 前段: `docs/handoffs/2026-08-02_2140_claude_migration-recovery-phase6g.md`
- 監査: `docs/audits/2026-08-02_tool_paths.md`

---

## 1. 再利用した既存Tool機能

監査の結論は「**塞ぐべき経路は無い**」だった。理由は構造的で、
そもそも tool calling / function calling を使っていない。LLM が返すのは
文章か JSON で、それを読んだコードが分岐して呼ぶ。**送信・削除・購入・
ゲーム操作は一つも接続されていなかった。**

だから今回は「危ない経路を塞ぐ」ではなく、**まだ何も無い所に、
最初から門を付けて繋ぐ**作業になった。塞ぐ対象が無いぶん素直だが、
逆に**新しく作る門が唯一の防壁**になる。

作り直さず、そのまま使ったもの:

| 既存 | どう使ったか |
|---|---|
| `decide_permission()` の7分類 | **判定表を増やさない。** `category=` を足して、ツール側の語彙を既存の `ActionCategory` へ写して呼ぶ |
| `ActionCandidate` / Action Selector | 計画の手順も**同じ候補の列**へ載せる |
| `CognitiveTrace` | 新しいログ基盤を作らず、項目を足すだけ |
| `mind.db` | 新しい DB を作らず、表を3つ足すだけ |
| `MigrationJobRunner` の起動ID方式 | `PROCESS_ID` で再起動を見分ける形をそのまま踏襲 |
| `dialogue_allows_tool` | **置き換えない。** これは検索専用の判断（ゲーム中は検索しない等の意味論が入っている）で、汎用の権限層ではない。**検索の追加条件として残す** |

---

## 2. Tool Capability分類

```
INTERNAL           自分の中だけ
READ_ONLY          外を読むだけ
LOCAL_REVERSIBLE   手元に書くが戻せる
EXTERNAL_WRITE     外部へ書く（戻せるとは限らない）
PERSON_DIRECTED    人へ届く（届いたら取り消せない）
DESTRUCTIVE        消す・買う・操作する
```

**`LOCAL_REVERSIBLE` も確認側へ倒してある。** 戻せることと、勝手に
やってよいことは別で、戻し方を知っているのはこちらだけ。ユーザーは
何が起きたか分からないまま残る。

登録したのは、監査で実在を確かめた4つだけ:

| tool_id | 操作 | 分類 |
|---|---|---|
| `web_search` | `search` | READ_ONLY |
| `game_state_read_only` | `read` | READ_ONLY |
| `vision_read_only` | `describe` | READ_ONLY |
| `test_scratch` | `write_note` / `notify` | LOCAL_REVERSIBLE / PERSON_DIRECTED、**両方 `dry_run_only`** |

**架空のツールを実経路へ載せない**（第27項）。
**本番の送信・投稿・削除・購入は一つも登録していない**（第28項）。

分類は `tools.py` の定数として持つ。**LLM の説明文からは決めない。**
「これは安全な操作です」とモデルが書けば安全になる仕組みにすると、
説明文が攻撃面になる。

---

## 3. Bounded PlanとAction Intent

```
GoalRecord            何を目指しているか
  └ BoundedPlan       そのために次の1〜3手で何をするか
      └ PlanStep
          └ ActionIntent   こうしたい（実行の許可ではない）
              ↓ 門
          ToolExecutionRequest  許可済み
```

`ActionIntent` と `ToolExecutionRequest` を分けたのが要点。混ぜると、
意図を作った時点で実行が確定してしまい、**間に確認を挟む場所が無くなる。**

* 上限 **3手**。4手以上は `too_many_steps` で拒否。
* **黙って3手へ詰めない。** `build_plan()` は5手でも5手のまま作り、
  Admission Gate が断る。切り捨てると「落とした」ことが誰にも伝わらない。
* `max_replans: 1`。
* 手は**1つずつ**。`plan.current` は未完了の先頭だけを返す。

### Action Selector への接続

計画は `plan_candidates()` で `ActionCandidate` になる。**そこから先は
他の行動と同じ扱い**——危険警告・相手の発話・自発発話と比べて、
Action Selector が選ぶ。**計画があるという理由だけで実行しない。**

副作用が重いほど基礎点が低い（READ_ONLY .58 → DESTRUCTIVE .30）。
確認を出す候補は最低 .55 まで持ち上げてある——**迷ったら聞く側へ
倒れてほしい。**

`EXECUTE_TOOL` の発話方針は `FORBIDDEN`。**実行は発話ではない。**
実行したことを話すかは、結果が出てから別の候補として並べ直す。

---

## 4. PermissionとConfirmation

### 権限

既存の `decide_permission()` をそのまま使う。`READ_ONLY` は自動、
それ以外は確認。**知らない操作も確認側**。

### 確認の拘束

```
confirmation_hash = SHA-256(
    tool_id | operation_id | effect_category | 正規化した引数 | 対象
)[:16]
```

宛先・本文・金額・ファイル・副作用分類のどれが変わっても別の値になる。
**実行の直前に照合する**ので、確認の後で引数だけ差し替える経路は通らない。

### 承認できる人

```
不明話者の「はい」        → approver_identity_unknown
競合している話者          → approver_identity_conflicted
別参加者の「いいよ」      → approver_not_requester
低confidenceの声紋一致    → low_approver_confidence（下限 .75）
Pendingが複数            → ambiguous_multiple_pending
時間切れ                 → EXPIRED（既定 120秒）
```

**Pending が複数ある時、単独の「はい」は使わない**（第13項）。ここで
先頭を選ぶと、送るつもりの無い方が送られる。そもそも Admission Gate が
`confirmation_in_flight` で2件目を開かせない。

身元が `UNKNOWN` / `CONFLICTED` の依頼者は、**確認を出す所まで行かない**
（`requester_unknown` で REJECT）。

### 使い切り

承認は**一度の実行で `CONSUMED`**。実行の**前**に印を付ける——後だと、
途中で落ちた時に「はい」が生き残る。

---

## 5. Tool Gate

**全ての実行がここを通る。** 迂回する枝を作らない——そういう枝は、
後から条件が増えた時に必ず更新から漏れる。

```
tool_registered            台帳にある
tool_enabled               設定で有効
operation_allowed          その操作が存在する
parameter_schema           引数が正しい（知らない引数は拒否。黙って落とさない）
effect_category_match      宣言と実物が一致（自称 read_only を通さない）
permission_valid           判定があり、この step のもの（流用しない）
confirmation_approved      要る場合は APPROVED
approver_matches_requester 承認者＝依頼者
confirmation_not_expired   期限内
confirmation_hash_match    確認した内容と同じ
plan_still_active          計画も手順も生きている
not_already_executed       同じ実行が済んでいない
```

一つでも満たさなければ拒否。プローブ `gate.checks` が
**12項目すべてに印が付くこと**を毎回確かめている（1つ消すと赤）。

---

## 6. 最初に接続したREAD_ONLYツール

`web_search` の `search` 1つだけ。`pipeline._bind_tool_runtime()` が
実物の `DeepSearch` へ束ねる。

```
明示的な依頼
→ USER_GOAL
→ 1手の Bounded Plan
→ READ_ONLY の Action Intent
→ Action Selector（候補の列）
→ Permission = ALLOW_AUTOMATIC
→ Tool Gate（12検査）
→ 実ツール
→ ToolResult
→ Outcome Verification
→ ユーザーへ
→ Goal COMPLETED
```

**この経路が実機で通ってから、確認が要る書き込みへ進む。**

---

## 7. Idempotency・Retry・Timeout

### 冪等

```
idempotency_key = SHA-256(plan_id | step_id | tool_id | operation_id | 正規化した引数)[:24]
```

**時刻も実行IDも混ぜない。** 混ぜると毎回違う値になり、二重送信が
素通りする（故障注入で確認済み）。

```
既に成功 → 再実行せず既存結果を返す
実行中   → mark_running が False。二重起動しない
失敗     → Retry Policy へ
```

### Retry

**自動でやり直すのは、副作用が無く・冪等が保証されているものだけ。**

| | |
|---|---|
| `web_search` | `auto_retryable = True`（最大2回） |
| `vision_read_only` | 読むだけだが `idempotent=False`（画面が変わる）→ Retry しない |
| 書き込み全部 | **Retry しない** |

### Timeout

```
読み取りが切れた   → TIMED_OUT（何も起きていないと言い切れる）
書き込みが切れた   → UNKNOWN_OUTCOME（届いたか分からない）
                     → 自動再実行しない
                     → 再計画も止める
```

### Cancel

実行は別スレッド。**音声会話を同期的に止めない**——数秒間ポッポの耳が
止まると、その間の呼びかけが全部落ちる。

止める要求の結果は4つを区別する: `CANCELLED` / `still_running` /
`not_cancellable` / `already_completed`。**止まっていないものを
「止めた」と言わない。** `test_scratch.notify` は `cancellable=False`。

---

## 8. Outcome Verification

**「成功が返った」は「効いた」ではない。**

| 方法 | 何を見るか |
|---|---|
| `OUTPUT_PRESENT` | 中身のある出力が返ったか（読み取りはこれで足りる） |
| `READBACK` / `FILE_EXISTS` | 読み直して確かめる |
| `PROVIDER_ACK` | 送信側が成功と外部参照を返したか |
| `NONE` | **確かめようがない** |

確かめられない時は `UNVERIFIED`——Step は完了にならず、Plan は
`PAUSED`、**Goal も完了しない。**

失敗した時は `REFUTED`。`test_a_failure_is_not_spoken_as_a_success` と
プローブ `outcome.no_fabrication` が、失敗が完了にならないことを固定
している。

---

## 9. 永続化と再起動時の処理

`mind.db` へ3表:

```
bounded_plans   plan_id / goal_id / status / replan_count / steps_json / …
confirmations   confirmation_id / confirmation_hash / requester / status / expires_at / …
tool_executions execution_id / idempotency_key / status / attempts / process_id / …
```

**引数の値は `tool_executions` へ入れない**（第30項）。記録してよい形
（`sensitive` を `<sensitive>` で伏せたもの）は `bounded_plans.steps_json`
にだけ置く。実行の記録から本文を復元できないようにしてある。

### 再起動後

```
起動 → mark_executions_unknown(PROCESS_ID)
     → 起動IDが違う running / pending → unknown_outcome

PENDING Confirmation      → 期限切れなら EXPIRED
APPROVED Confirmation     → **EXPIRED**（restart_invalidated）
READ_ONLY の未完了        → resume_candidate（状態確認のうえ人が決める）
EXTERNAL_WRITE の実行中   → unknown_outcome_no_retry
```

**承認済みを再起動でまたがせない。** 状況が変わっているかもしれないのに、
起動しただけで外部操作が走る経路になる。

**再起動だけを理由に、副作用操作をやり直さない。**

---

## 10. 追加テストと結果

- `tests/test_tool_execution.py` — **82件**
- 全体 **2542 passed / 17 subtests**、pyflakes clean
- 診断 **要注意 0 件**（warm 27.5ms / 90プローブ。上限 250ms）

### 故障注入（テスト側）

| 切ったもの | 赤くなったもの |
|---|---|
| 確認の hash 照合を外す | `test_changing_a_parameter_invalidates_the_confirmation` |
| 曖昧な「はい」で先頭を選ぶ | `test_a_bare_yes_is_refused_when_two_things_are_pending` |
| 冪等キーに時刻を混ぜる | 3件（うち1件は下記の修正で拾えるようになった） |
| 書き込みも自動Retryにする | `test_a_write_is_never_retried_automatically` |
| 宣言と実物の照合を外す | `test_a_tool_cannot_declare_its_own_effect_category` |
| 不明話者でも承認できる | `test_an_unknown_speaker_cannot_approve_a_side_effect` |

### 故障注入（プローブ側）

| 切ったもの | 赤くなったもの |
|---|---|
| 確認の使い切りを止める | `confirmation.single_use` |
| 宣言の照合を外す | `tools.effect_declaration` |
| 再起動で自動再開する | `execution.restart` |
| 門の検査を1つ削る | `gate.checks` |

### 自分のバグ1件

> **`test_the_same_request_runs_only_once` が効いていなかった。**
> 同じ `ToolExecutionRequest` オブジェクトを2回渡していたので、鍵は
> オブジェクトの中に固定されており、**鍵の作り方が壊れていても
> （時刻が混ざっていても）通ってしまう**。冪等キーに時刻を混ぜる故障
> 注入をしても、このテストだけ緑のままだった。門を2回別々に通す形へ
> 直したら、注入で赤くなるようになった。

### 追加した15プローブ（層「道具」）

```
tools.registry                  副作用分類がコード側にあるか
tools.effect_declaration        ツールが自分で「安全」と名乗れないか
plan.bounded                    4手以上を断れるか・黙って切っていないか
plan.selector                   計画が候補の列へ載るか
confirmation.binding            確認が引数へ拘束されているか
confirmation.identity           別人・不明・弱い一致を断れるか
confirmation.single_use         一度で使い切るか
gate.checks                     12項目すべて検査しているか
gate.write_needs_confirmation   確認なしの書き込みを止められるか
execution.idempotency           同じ鍵が変わらないか・二重起動しないか
execution.no_write_retry        書き込みを自動でやり直さないか
outcome.verification            証拠なしで Goal を完了にしないか
outcome.no_fabrication          失敗を成功にしないか
tool.untrusted_output           結果の中の命令を実行しないか
execution.restart               再起動で勝手に再開しないか
```

`execution.restart` は `MemoryStore(":memory:")` を使う。3秒ごとに回る
点検が一時ファイルを作ると会話の邪魔になる（Phase 6G で踏んだ）。

---

## 11. 実機確認手順

**この順でしか上げない。** 各段でひとつ前が緑になってから次へ。

### 段階1: 計画とTraceだけ

1. `planning.enabled: true` / `planning.bounded_plans_enabled: true`。
2. **会話が変わらないこと。** ツールは動かない。
3. 🩺の「道具」層が全部緑。`plan.selector` に候補の点が出る。
4. Trace に `plan_id` / `admission` が入る。

### 段階2: READ_ONLY を1種類

5. `tool_execution.enabled: true` / `read_only_enabled: true` /
   `tools.web_search_enabled: true`。
6. 「◯◯について調べて」と**明示的に**言う。
7. 見るところ:
   - **検索中も呼びかけに反応すること**（実行が会話を止めていない）
   - 結果を**自分の体験として語っていない**こと
   - 同じことを2回頼んだ時、2回目が既存結果を返すこと
8. 🩺で `tool_gate_result=allow` / `tool_result_status=succeeded`。

### 段階3: Outcome Verification

9. **わざと繋がらない状態**（機内モード等）で調べさせる。
10. **「調べておいたよ」と言わないこと。** ここが一番大事。
11. 🩺で `outcome_verified=false`、Goal が完了になっていない。

### 段階4: 永続化

12. `tool_execution.persistence_enabled: true`。
13. 検索を1回。**アプリを再起動。**
14. 🩺で記録が残っていること。**勝手に再検索していないこと。**

### 段階5: 試験用の書き込み＋確認

15. `tools.test_scratch_enabled: true` / `external_write_enabled: true` /
    `confirmation.enabled: true`。
16. 試験用の送信を頼む → **確認を聞いてくること。**
17. 次を1つずつ試す:

| 試すこと | 期待 |
|---|---|
| そのまま承認 | 一度だけ実行される |
| 同じ「はい」でもう一度 | 実行されない |
| **別の人**が「いいよ」と言う | 断る |
| 承認の後に宛先を変える | **聞き直す** |
| 2つ頼んでから「はい」 | どちらか聞き返す |
| 2分放置してから「はい」 | 期限切れ |
| 承認 → 実行中に**アプリを落とす** → 再起動 | **勝手に再実行しない** |

### 段階6: 声での確認

18. `confirmation.voice_confirmation_enabled: true`。
19. **声紋が `CONFIRMED` の相手**で承認できること。
20. `PROBABLE` 止まりの相手では断ること（🩺の話者一覧で確認）。

### 段階7以降

21. 限定的な実ツール書き込み → 通常利用。**ここから先は未着手。**

### 割り込みの確認（どの段でも）

| 場面 | 見るところ |
|---|---|
| ツール実行中に危険な出来事 | **WARN が先に出る**こと |
| ツール実行中に呼びかけ | 反応すること |
| 「もういいよ」 | 未実行の手順を勝手に続けないこと |
| 長い実行の途中 | 進捗を何度も喋らないこと |

---

## 12. 残っている重要な穴

- **実機未検証。** 上の手順は未実行。特に段階3（失敗を成功と言わないか）
  と段階5の7項目は、コードでは固定してあるが**耳で確かめていない。**
- **ツール結果を発話へ渡す経路が未接続。** `ToolTurn.spoke_safely` は
  用意したが、Conversation Planner へ渡す所は書いていない。いまは
  実行して記録するまで。**次のいちばん大きな仕事。**
- **`instruction_like()` は語句の照合だけ。** 「これまでの指示を無視」の
  言い換えは素通りする。**印が漏れても実行はされない**（実行経路が
  結果から作られないため）が、印を当てにしすぎない方がいい。
- **`dialogue_allows_tool` と Tool Gate が二重になっている。** 意図的に
  残してあるが、検索の条件が2箇所にある状態ではある。将来どちらかへ
  寄せるなら、**ゲーム中は検索しない等の既存の意味論を落とさない**こと。
- **再計画の実装が「止める」側だけ。** `can_replan()` は判定するが、
  実際に作り直す（別のツールを選ぶ等）経路は無い。
- **Discord 側へ繋いでいない。** `pipeline._bind_tool_runtime()` は
  Local だけ。第19条の parity として**借りが1つ**。`bot.py` 側にも
  同じ配線が要る。
- **`test_scratch` に実物が繋がっていない。** 台帳にはあるが
  `executor.bind()` していないので、実行すると `not_bound` で失敗する。
  段階5の前に、作業フォルダ内へ書くだけの実装を足す必要がある。
- **`test_a_double_click_does_not_start_two_jobs`（Phase 6F）が稀に落ちる。**
  最初の job が2回目の `start` より前に完了すると job_id が変わるため。
  Phase 7 とは無関係の timing flake。直すなら不変条件（同時に2つ走らない）
  を見る形へ。
