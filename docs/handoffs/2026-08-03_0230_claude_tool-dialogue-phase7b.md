# Phase 7B: ツールの結果を、会話として返すまで

- 日時: 2026-08-03 02:30 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-03 02:30 の項
- 状態: 自動回帰済み。**実機未検証**（段階3と段階5は耳で確かめるまで完了扱いにしない）
- 前段: `docs/handoffs/2026-08-02_2355_claude_tool-execution-phase7.md`

---

## 1. ToolResultが以前止まっていた地点

`ToolRuntime.execute()` の最後。

```
実行 → ToolResult
     → verify_result()
     → apply_tool_outcome()      ← Plan / Goal / World State は更新されていた
     → _persist_plan()
     → return turn               ← **ここで終わり**
```

`ToolActionOutcome` まで作られ、`mind.db` にも残っていた。だが
**それを読む側が一人もいなかった**。`ToolTurn` を受け取るのは
テストだけで、Action Selector にも Planner にも Speech Gate にも
繋がっていない。

監査で確かめた各地点:

| 場所 | 状態 |
|---|---|
| `ToolResult` の到達点 | `ToolTurn.result`。**呼び出し側が読んでいない** |
| `ActionOutcome` の入口 | `apply_tool_outcome()` は動いていた。世界状態へは戻る |
| Planner へ渡せる構造 | `working_memory` の dict。**ツール用の口は無かった** |
| `SpeechRequest` の発話元 | 7種。ツール結果に相当するものが無い |
| Speech Gate の許可表 | `PROACTIVE_ALLOWLIST` のみ。結果報告用が無い |
| Local 経路 | `pipeline._bind_tool_runtime()`（Phase 7 で作った） |
| Discord 経路 | **無かった**（第19条の借り） |
| DAVE / Opus / TTS 出口 | 既存のまま。ツールからは触れない |
| `test_scratch` | 型だけ。`dry_run_only`、実物は未接続 |
| Permission / Confirmation の紐付け | 人物のみ。**会話・チャンネルは持っていなかった** |
| Migration Job の二重起動 | `busy` だけを見ており、完了直後の2回目が通る |

---

## 2. ToolOutcomeEventと認知経路への接続

```
ToolResult
→ 1. 保存
→ 2. Outcome Verification
→ 3. Plan Step 更新
→ 4. Goal / Obligation / World State 更新
→ 5. ToolOutcomeEvent 発行
→ 6. Action Selector（候補の列）
→ 7. Planner（6項目だけ）
→ 8. Speech Gate
→ 9. 実出力
```

**発話してから成功状態へ更新する順序にしていない。** 逆にすると、
途中で落ちた時に「言ったのに記録が無い」が残る。

`ToolOutcomeEvent` は仕様どおりの項目を持つ。`user_safe_summary` は
**「確認できた」「できなかった」程度の短い事実**であって、ポッポの
台詞ではない。ここで文章を作ると毎回同じ固定文になる——言い方は
Conversation Planner と Surface Realizer の仕事。

---

## 3. ツール出力の信頼境界

```
ToolPayload:
    structured_data        ツールが返した生の dict
    raw_output             400字まで。**ここから外へ出ない**
    verification_evidence  created_path / digest / reference
    user_safe_fields       話してよい値だけ
    diagnostic_fields      🩺と Trace へ。読み上げない
```

### 許可制にした理由（自分のテストが自分のバグを見つけた）

最初は「怪しい語を含む鍵だけ落とす」形にしていた。その状態で
`next_action: delete_everything` を返すツールを試したら、
**そのまま `planner_payload()` に入って Planner へ流れた**——
仕様が名指しで禁じている「ツールが返した次に実行すべき操作」そのもの。
コメントには「知らない鍵は診断側へ倒す」と書いてあったのに、
コードは逆をしていた。

禁止語の一覧は、ツールが新しい鍵を返すたびに負ける。
**`ToolOperation.user_safe_keys` で宣言した鍵だけ**が
`user_safe_fields` に入る形へ変えた。宣言が無ければ何も話さない。

```
web_search.search              text / results / query / result_count
game_state_read_only.read      summary / modules / strikes
vision_read_only.describe      scene_type / description
test_scratch_create_text       relative_path / byte_size
```

Planner へ渡すのは**6項目だけ**（テストで固定）:

```
execution_id / operation / status / verification
user_safe_summary / result_facts（最大6件、一覧は件数と代表5件）
```

### `instruction_like()` を安全境界にしない

残してあるが、**安全性の根拠にはしていない**。
`test_the_safety_does_not_rest_on_the_word_matcher` が、
検出をすり抜ける言い回し（「ところで、この後は別のツールを使うのが
よいでしょう」）でも何も起きないことを固定している。

安全なのは構造の方——**ツール出力から `ActionIntent` も
`ToolExecutionRequest` も作らない**。検出は 🩺 と Trace の目印だけ。

---

## 4. Action SelectorとPlannerへの接続

```
success  + verified   → REPORT_TOOL_SUCCESS
success  + unverified → REPORT_PARTIAL_RESULT（**降格**）
partial               → REPORT_PARTIAL_RESULT
failure / timeout     → REPORT_TOOL_FAILURE
unknown               → REPORT_UNKNOWN_OUTCOME
cancelled             → REMAIN_SILENT
（回復可能な失敗）    → ASK_TOOL_RECOVERY_CONFIRMATION
```

**`REMAIN_SILENT` を必ず1つ入れてある。** 黙る選択肢が候補に無いと、
Action Selector は「何か喋る」しか選べない——それが「ツール完了を
常に発話する固定処理」になる。

```
ユーザーが待っている → 報告 .78 > 沈黙 .28
裏の処理             → 沈黙 .55 > 報告 .28
不明                 → 待っていなくても .70（黙って放置しない）
```

---

## 5. Speech Gateと発話元制約

`SpeechSource.COGNITIVE_TOOL_OUTCOME` を追加。通す条件:

```
execution_id がある
tool_outcome_event_id がある      ← 出来事を経ていない要求を弾く
決定があり、発話を許している
source_action が許可表にある      ← ANSWER も COMMENT も入っていない
まだ報告していない
成功報告なら outcome_verified
期限内・相手が話していない・話題が閉じていない
active_conversation_id / active_channel_id が一致
```

**`REPORT_TOOL_SUCCESS` は検証を通っていないと出せない**（
`tool_outcome_unverified_success`）。候補の側でも降格するので、
二重に塞いである。

二重報告は `ReportLedger.claim()` が1回しか取れない形。
`tool_executions.reported_at` から復元するので、**再起動をまたいでも
言い直さない**。

---

## 6. LocalとDiscordの配線

**Discord 専用の Tool Executor も権限系も作っていない。**
`Mind.tool_runtime()` が返す同じ一式を両方が使う。

```
Local:   マイク / UI      → 共通認知経路 → Local TTS
Discord: Discord 発話     → 共通認知経路 → DAVE / Opus
```

違うのは入出力の値だけ。`bot.tool_context()` が Discord 側の宛先を組む:

```
guild_id / channel_id / discord_user_id
conversation_id = "discord:<guild>/<channel>"
origin = "discord"
```

**人物統合が確定していない時に `person_id` を推測しない。**
確実に分かるのは Discord の識別子なので、そちらへ結び付ける。

照合は2箇所:

* `ConfirmationStore.approve()` — 依頼者・会話・チャンネルが一致しないと
  `approver_not_requester` / `conversation_mismatch` / `channel_mismatch`
* `check_speech()` — いま繋がっている会話・チャンネルと一致しないと出さない

---

## 7. test_scratchの実書き込み

```
tool_id        test_scratch_create_text
operation      create
effect         LOCAL_REVERSIBLE
verification   FILE_EXISTS（**作った物を読み直して確かめる**）
```

`tools.test_scratch_root` が空なら、`test_scratch_enabled` に関係なく
**ツールごと繋がらない**。既定の書き先を勝手に決めると、「試したつもり」が
本物のプロジェクトファイルの隣に落ちる。

`ToolOperation.idempotent = False`。同じ内容でもう一度呼ぶと
**上書きせず失敗する**ので、冪等ではない。自動 Retry もしない。

---

## 8. Confirmationとパス安全性

### パス

```
絶対パス（/ ~ C:）        → absolute_path_not_allowed
.. を含む                 → path_traversal_not_allowed
深さ4以上                 → too_deep
.txt / .md / .log 以外    → suffix_not_allowed
隠しファイル              → hidden_file_not_allowed
記号・NUL                 → invalid_character / null_byte
symlink そのもの          → symlink_not_allowed
途中の階層が外を指す      → symlink_escapes_root
正規化後に root の外      → outside_scratch_root
既にある                  → file_already_exists（**上書きしない**）
8KB 超                    → content_too_large
```

**文字列の判定だけでは足りない。** `notes/link/a.txt` の `link` が
外を指す symlink なら、文字列はきれいなまま実体が root の外になる。
存在する所まで実体化してから確かめている。

書き込みは `O_CREAT | O_EXCL`。`exists()` の後に誰かが作っても失敗する。

### 確認の拘束

`confirmation_hash` は Phase 7 の内容に加えて
**依頼者・会話・チャンネル**も覆う。同じ引数でも、別の人・別の場所
からの依頼は別の操作。

`ambiguous_utterance=True` の承認は受けない——ASR で聞き取れたか
怪しい「はい」を確認済みにしない。書き込みは戻せないので、
聞き直す方が安い。

### 可逆性

```
created_path / content_digest / created_by_execution_id / created_at
```

`rollback()` が消すのは**その execution_id が作った、中身が変わって
いない、root 内の、同じパスのファイル**だけ。

```
作成者が分からない        → unknown_creator
パスが変わった            → path_changed
既に無い                  → already_gone
中身が変わっている        → content_changed（**別の誰かが使っている**）
```

どれも**消さない**。「掃除しておいた」で消える方が、残っているより困る。

---

## 9. 再計画の許可・禁止条件

```
許可: target_not_found / read_only_alternative_available /
      tool_temporarily_unavailable / missing_information /
      world_state_changed / assumption_refuted
      **かつ副作用が INTERNAL または READ_ONLY**
      **かつ side_effect_confirmed でない**
      **かつ replan_count < max_replans（既定1）**
      **かつ同じ理由を繰り返していない**

禁止: unknown_outcome / permission_denied / requester_unknown /
      user_cancelled / goal_closed / confirmation_expired /
      safety_boundary / destructive_operation / side_effect_possible
      **書き込み系の副作用は分類そのもので禁止**
```

**失敗した書き込みを別の書き込みへ切り替えない。**
「1通目が届かなかったから別の宛先へ」は、こちらが決めてよいことではない。

作り直した計画は `replan_of` に元の `plan_id` を持ち、`goal_id` は
変えない。**Plan Admission と Permission をもう一度通す**（テストで固定）。

---

## 10. Migration Jobのflake修正

**sleep を伸ばしていない。** 同一性の正本を作った。

```
operation_key = "<operation>|<sorted targets>"
```

`start()` はロックの中で get-or-create（compare-and-set）する。
同じ鍵の作業が既にあれば、**走っていても、たった今終わっていても**、
新しく作らずそれを返す。

以前のテストが不安定だった正体はここ。`busy` だけを見ていたので、
1件目が終わる前か後かで答えが変わっていた。押した人にとっては
どちらも同じ1回。**時間ではなく作業の同一性**で決める。

テストは `threading.Barrier` で2本のスレッドを同時に放つ形と、
**完全に終わらせてから**押す形の2本。どちらも sleep に依存しない。

`migration.single_start` プローブは、実際に2回叩いて1件になることと、
別の対象なら別の作業になることを見る（**読むだけの点検にしない**）。

---

## 11. 追加テストと結果

- `tests/test_tool_dialogue.py` — **73件**（仕様の18シナリオを全て含む）
- 全体 **2615 passed / 17 subtests**、pyflakes clean
- 診断 **要注意 0 件**（warm 38ms / 100プローブ。上限 250ms）

### 追加した8プローブ

```
outcome.reaches_dialogue      結果が会話経路へ戻るか（黙る候補もあるか）
outcome.no_direct_speech      出来事と決定を経ないと出せないか
outcome.unverified_not_spoken 未検証で「できた」と言わないか
outcome.single_report         同じ結果を二度言わないか
outcome.trust_boundary        宣言していない結果を渡していないか
outcome.delivery_target       頼まれた場所へ返すか
confirmation.channel_binding  別チャンネル・曖昧な返事を確認にしないか
scratch.path_safety           書き先が決めた場所から出ないか
replan.bounded                読み取りの失敗だけ・最大1回か
migration.single_start        同じ移行を二重に始めないか
```

### 故障注入

| 切ったもの | 赤くなったもの |
|---|---|
| 未検証でも成功と言えるようにする | テスト1件 + `outcome.unverified_not_spoken` |
| 二重報告を許す | テスト3件 + `outcome.single_report` |
| 宣言していない鍵も話す | テスト2件 + `outcome.trust_boundary` |
| 別チャンネルへも返す | テスト1件 + `outcome.delivery_target` |
| 書き込みも作り直す | テスト5件 + `replan.bounded` |
| 同一性で束ねない | テスト1件 + `migration.single_start` |

### 自分のバグ2件

1. **`redact()` が禁止語の一覧だった**（§3）。`next_action:
   delete_everything` が Planner へ流れた。コメントは「知らない鍵は
   診断側」と書いてあったのに、コードは逆。許可制へ変えた。
2. **timeout の `UNKNOWN_OUTCOME` 判定が `IRREVERSIBLE` の3種だけを
   見ていた。** `LOCAL_REVERSIBLE` が漏れていて、**ファイルは作られた
   かもしれないのに `TIMED_OUT`（何も起きていない）** になっていた。
   戻せるかどうかと、起きたかどうかは別の話。`side_effect_free` 基準へ。

> `migration.single_start` プローブも一度作り直している。最初は
> `start()` のソースに `_by_key` があるかを見るだけで、故障注入
> （`_by_key.get()` を `None` にする）に**気づかなかった**。
> 実際に2回叩く形にした。

---

## 12. レイテンシ

`ToolTurn.latency_ms` に記録:

```
outcome_verification_ms
tool_outcome_event_ms
tool_execution_ms                 ← **外部の待ち時間。内部処理と分ける**
tool_result_action_selection_ms
```

Trace 側には Phase 7 の8項目がそのまま残っている。

`planner_payload()` の長さをテストで上限に固定してある（1200字未満）。
**長い raw_output を LLM へ入れて遅らせない。**

---

## 13. 実機確認手順

前段（Phase 7 の段階1〜4）が緑になってから。

### 段階1: Local の読み取り

1. `tools.result_dialogue_enabled: true`。
2. マイクから「作業フォルダの中を確認して」等、**明示的に**頼む。
3. 見るところ:
   - 実ツールが**1回だけ**動く（🩺の `execution.idempotency`）
   - 件数か短い結果が**発話される**（ここが Phase 7 との違い）
   - **raw ログ全文を読み上げない**
   - 内部IDやスタックトレースを読まない

### 段階2: Local の失敗

4. 存在しない対象を指定する（機内モードでも可）。
5. 見るところ:
   - **「調べておいたよ」と言わない**
   - 短く失敗を伝える
   - **勝手に別の操作を始めない**
   - 🩺で `tool_result_status=failed` / `outcome_verified=false`

### 段階3: Local の音声品質（**耳で確認**）

6. 何度か繰り返して、次を聞く。

| 見るところ | |
|---|---|
| 報告が長すぎないか | 一覧を全件読んでいないか |
| 内部用語を読んでいないか | `execution_id` や `verified` が声に出ていないか |
| 完了前に完了と言っていないか | |
| 同じ結果を二度言っていないか | |
| 次の発話を邪魔していないか | 話しかけた時に被らないか |

**こちらでは「自然だった」と判断できない。** 聞いた印象を教えてほしい。

### 段階4: test_scratch の書き込み

7. `tools.test_scratch_root` に**専用のフォルダ**を設定（例:
   `C:\Users\coala\Desktop\Grok_API\Codex\AItuber\scratch`）。
   **プロジェクトのファイルがある場所を指定しないこと。**
8. `tools.test_scratch_enabled: true` /
   `tool_execution.external_write_enabled: true` /
   `confirmation.enabled: true`。
9. テキストファイルの作成を頼む。
10. 見るところ:
    - **パスと内容の確認が出る**（操作・場所・内容の要約・上書きしない）
    - 明示的に承認するまで作られない
    - 承認後、**専用 root の中に1件だけ**できる
    - 「はい」を2回言っても**1件だけ**
    - 同じ名前でもう一度頼むと**上書きせず失敗**する
    - 承認の後で名前を言い直すと**聞き直す**

### 段階5: Discord 実機（**耳で確認**）

11. `tools.discord_enabled: true`。
12. Discord 音声で読み取り専用の操作を頼む。
13. 見るところ:
    - **同じ Tool Gate を通る**（🩺のトレースで `gate.result=allow`）
    - 結果が**同じチャンネルへ**返る
    - **別のユーザーの返事では確認が成立しない**
    - **別のチャンネルの返事でも成立しない**
    - 発話が DAVE / Opus 経路から**1回だけ**出る
    - Local 側へ漏れていない

### 段階6: bounded replan

14. `tools.bounded_replan_enabled: true`。
15. 読み取りが失敗する状況を作る。
16. **「もう一度やってみる?」と聞くこと**（勝手にやり直さない）。

**段階3と段階5は、実際に耳で確かめるまで完了扱いにしない。**

---

## 14. 旧経路への戻し方

上から順に落とすほど、前の状態へ戻る。

| 落とすフラグ | 戻る先 |
|---|---|
| `tools.bounded_replan_enabled` | 失敗したら止まるだけ |
| `tools.discord_enabled` | Discord から道具を使えない（Local のみ） |
| `tools.test_scratch_enabled` または `test_scratch_root` を空に | 書き込みが繋がらない |
| `tools.result_dialogue_enabled` | **Phase 7 の状態**——実行して記録するが会話へ戻らない |
| `tool_execution.enabled` | 門が全部拒否（Phase 6 の状態） |
| `planning.enabled` | 計画そのものを作らない |
| `cognition.enabled` | 認知層ごと止まる（従来の会話経路） |

`tools.result_dialogue_enabled: false` にすると、実行と記録は続くが
候補が作られず、`ReportLedger` に `result_dialogue_disabled` として
抑制理由が残る（テストで固定）。**黙ったのか壊れたのかが区別できる。**

DB の列は追加のみで、落としても既存データは壊れない。

---

## 15. 残っている重要な穴

- **実機未検証。** 段階3と段階5は耳で確かめていない。
- **`ToolOutcomeEvent` → Conversation Planner の実プロンプト組み立てが
  未接続。** `planner_payload()` は用意し、6項目に絞ってあるが、
  それを `working_memory` へ入れて Planner を呼ぶ側は書いていない。
  **いまは候補を作って Speech Gate を通すところまで。**
  次の作業はここ。
- **Action Selector の実選択を通していない。** `report_candidates()` は
  候補を返すが、`CognitiveKernel` の列へ合流させる配線は
  呼び出し側（pipeline / bot）に無い。テストは候補と Gate を
  直接検証している。
- **Discord の `tool_context()` に呼び出し側が無い。** 型と組み立ては
  あるが、実際の発話イベントから渡す所は未接続。第19条の借りが
  半分残っている。
- **`user_safe_keys` の宣言は4ツール分だけ。** 新しいツールを足す時に
  宣言を忘れると、結果が全く話されなくなる（安全側に倒れるが、
  「壊れている」ように見える）。🩺 に出していない。
- **`rollback()` を誰も呼んでいない。** `ScratchWriter` に実装はあるが、
  UI からも会話からも辿れない。
- **`ReportLedger` は永続化していない。** `reported_at` から復元するが、
  ledger 自体はメモリ。同一プロセス内では正しく、DB との同期は
  `mark_execution_reported()` 頼み。
- **長い一覧の「画面表示へ回す」が未実装。** 件数と代表5件までは
  絞るが、残りをユーザーが見る手段がない。
