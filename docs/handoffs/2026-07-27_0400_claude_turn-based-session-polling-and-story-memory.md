# AI作業Handoff

- 担当AI: Claude (Cowork / claude-opus-5)
- Role: investigation / implementation / test
- Task: ターン制セッションの区間ポーリング暴走と、文脈切り詰めによる物語の消失
- Started At: 2026-07-27 (JST)
- Finished At: 2026-07-27 (JST)
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 実働ルート直接 (R-001のとおりGit未管理)
- Work Lock: 同セッション内で連続作業

## 目的

ユーザー報告「選択を回答する毎に普通の応答内容みたいになっている。そして遂に続きを話さなくなった」を、実機ログ（2026-07-26 23:31〜23:37）から根本原因まで追う。

## 作業開始時の状態

- 直前の `2026-07-27_0230` の修正が実機で動作していることをログで確認:
  - `23:31:37,315 Directive created id=747033ec602d interaction=NARRATION`
  - `23:31:37,711 Directive rekeyed id=747033ec602d local:user → speaker:1 (segments=0)`
  - **`Directive created` はセッション中1回だけ**。孤児化は解消している。
- テストbaseline: 751 passed / 3 skipped

## 調査結果

### 1. 「遂に続きを話さなくなった」— 区間生成の暴走

```
23:35:40,225  plan=exchange_naturally_closed        ← 「うんうん。」を沈黙と判定
23:35:46,228  LLM生成 初トークン=1681ms
23:35:52,894  LLM生成 初トークン=1516ms
23:35:57,731  LLM生成 初トークン=1552ms
   … 約5秒間隔で 23:37:19 まで継続（およそ20回）
23:37:26,042  ユーザー「えーで、続きは?」
```

チャット画面に現れたのは、この90秒間で**1件だけ**。20回近い生成の大半は発話されていない。

原因の連鎖:

1. 「うんうん」は Turn Closure が沈黙と判定 → `note_suppressed_user_turn`（2026-07-27 00:30に私が追加）が `WAITING_FOR_USER` を解除して `ACTIVE` にする。
2. tickごとに `due_directive` がDirectiveを返し、区間生成が走る。
3. NARRATION は `needs_user_input=True`。モデルは正しく「今はプレイヤーの手番だ」と判断して `WAIT` を返す。
4. `continuation.py` の `WAIT` 分岐は `waited_segment_count` を増やすだけで**ステータスを変えない**。`ACTIVE` のままなので次のtickでまた聞く。

Ollamaは直列（D-017）なので、この空回りがユーザーの次の発話の前に並ぶ。「続きを話さなくなった」の実体は、GPUが無意味な生成で埋まっていたこと。

`WAITING_FOR_USER` の解除自体は正しい（さもないと00:30以前の停止に戻る）。誤っていたのは、**ターン制セッションにおける `WAIT` の意味**を「あとで再試行」と解釈していたこと。ターン制での `WAIT` は「相手の手番」という状態であって、リトライではない。

### 2. 「普通の応答内容みたいになっている」— 物語が履歴から消えている

```
23:33:18  文脈長のため古い会話を 9件除外 (見積 9552tok / 予算6912tok / ctx=8192)
23:34:24  13件除外 ( 9958tok)
23:35:20  17件除外 (10306tok)
23:37:26  20件除外 (10476tok)
```

ターンを重ねるほどプロンプトが膨らみ、切り詰め件数が単調に増える。開始12ターン目には**冒頭の場面設定が履歴から消えている**。

一方 systemプロンプト（ペルソナ）は `context_budget.fit_messages` が絶対に落とさない。結果として、残るのは「ただ質問に答えるだけのアシスタントにならない。自分の感想を一言混ぜる」といった人格指示ばかりになり、GMのナレーションではなく「優しいね！きみならきっと…」という友達のコメントに寄る。ユーザーの観察は正確だった。

これは R-027（P1、セッション要約が未実装）として記録していたものが、実際に効いた形。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/continuation.py` | `WAIT` かつ `needs_user_input` なら `WAITING_FOR_USER` へ遷移 | ターン制での「待つ」は状態であってリトライではない |
| `neuro_voice/dialogue/directive.py` | `opening_note` / `progress_notes` と `record_progress()` を追加 | 切り詰めで消える履歴とは別に、物語の筋をDirective側へ持つ |
| `neuro_voice/dialogue/continuation.py` | `note_reply` と区間SPEAK時に `record_progress()` | 発話した内容だけを記録する（生成しただけのものは残さない） |
| `neuro_voice/dialogue/intent_plan.py` | `directive_prompt_block` へ「この依頼の始まり」「ここまでの流れ」を追加 | 履歴が切り詰められてもDirectiveブロックは残る |
| `tests/test_turn_based_session.py` | 新規13件 | 下記テスト節 |

### 記録の方式

- **モデルを使わない。** 要約のためのLLM呼び出しをターンごとに足すと、直列なローカルLLMでは応答が確実に遅くなる（D-017）。発話済みテキストを110文字で切って保持する決定的な方式にした。
- **冒頭は永久に保持する。** 物語を定義するのは最初の場面なので、`opening_note` は上書きしない。
- **以降は直近6件の窓。** 同一行の重複は記録しない。
- コストはおよそ数十〜200トークン。`num_ctx 8192` に対して許容範囲。

## 変更しなかったもの

- `context_budget` の切り詰め方針。systemを落とさないのは正しく、問題は「落とされる側に物語があった」こと。
- ペルソナの発話原則（`memory/persona.py`）。GMらしさが薄れる原因は人格指示ではなく、物語側の情報が消えていたこと。ここへ「TRPG中は感想を言うな」といった条件を足すのは D-016 が禁じている方向。
- `note_suppressed_user_turn` の `resume()`。これ自体は必要で、暴走の原因は `WAIT` の解釈側だった。

## 設計判断

1. **`WAIT` の意味はモードによって違う。** 一人語りの「間を置く」と、ターン制の「相手の番」は別物。前者はリトライ、後者は状態遷移。同じアクション名に両方の意味を持たせたのが誤りだった。
2. **忘れる仕組みがあるなら、忘れてはいけないものを別に持つ。** 文脈予算は正しく動いていた。足りなかったのは、切り詰めの影響を受けない場所に筋を置くこと。
3. **要約にモデルを使わない。** 一番素直な実装はターンごとの要約生成だが、直列LLMでは前景の応答時間に直接効く。「そもそも呼ばない」を先に検討する（D-017）。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/ -q`（sandbox / Python 3.10 + StrEnum shim、音声・GUI依存4件除外） | 764 passed, 3 skipped, 11 subtests | 全実行ログ確認 |
| `tests/test_turn_based_session.py`（新規13件） | pass | 修正を戻すと**3件が失敗**することを確認済み |
| `test_local_audio_liveness.py` + `test_discord_screen_share.py`（sounddeviceスタブ下） | 21 passed | Codexの視覚系へ回帰なし |
| `python3 -m compileall neuro_voice` | exit 0 | — |

### 修正前に失敗することを確認したテスト

- `test_a_turn_based_session_stops_polling_when_told_to_wait`
- `test_replies_are_recorded_as_progress`
- `test_the_prompt_carries_the_story_forward`

`test_a_monologue_told_to_wait_keeps_its_turn` は逆方向の保護。ラジオが1回の `WAIT` で番組を止めないことを固定している。

- 未実施: **実機でのTRPG通し確認**、長時間セッションでの筋の保持
- Performance p50/p95: 未計測

## 設定・Migration

- Config: 変更なし。
- DB schema: 変更なし。`opening_note` / `progress_notes` はDirectiveのメモリ上フィールドで、既存の永続データに影響しない。
- 再起動: **GUI再起動が必要**。

## Rollback

- ポーリング: `continuation.py` の `if directive.needs_user_input:` ブロックを削除すると従来動作。
- 物語の記録: `intent_plan.py` の2ブロックを削除すればプロンプトから消える。`record_progress` は残っていても副作用がない。

## 失敗した試行

**00:30に入れた `note_suppressed_user_turn` が、この暴走の引き金だった。**

「沈黙したターンでもDirectiveへ手番を返す」は正しい。しかし手番を返した先で、ターン制セッションが `WAIT` を返し続ける経路を確認していなかった。片方の状態遷移だけを見て、その先の定常状態を見ていない。

同じ形の見落としが今日3回続いている（デバイス監視の既定値、VAD時点の破棄、今回）。共通しているのは、**「1回の遷移が正しいか」だけを検証して「繰り返したときに落ち着くか」を見ていない**こと。次からは、状態を変える変更を入れたら「この状態が続いたら何が起きるか」を必ず1問追加する。

なお R-027（セッション要約）は2026-07-26にP1として記録しながら着手していなかった。実害が出るまで放置した形になっている。

## 憲法・ADRへの影響

- 第17条（安全な失敗）: 同じフォールバックを無限反復しない、に該当する違反だった。
- 第16条（GPU・遅延）: 背景処理が会話中にGPUを占有し続けない、に該当。
- 第3条 / D-017: 要約をモデルではなく決定的処理で行う判断の根拠。
- R-026 / R-027 へ本件を追記。

## 次の担当への注意

- `SegmentActionType` の分岐へ手を入れるときは、そのアクションが**モードによって意味が変わらないか**を確認すること。今回は `WAIT` がそれだった。
- `record_progress` にLLM呼び出しを足さないこと。ターンごとの追加呼び出しは前景の応答時間に直接乗る。
- `progress_notes` の窓（6件）を増やす前に、トークン見積りへの影響を `context_budget` のログで確認すること。
- 実機受入項目: (a) TRPGで10ターン以上進めて冒頭の設定が保たれるか、(b) 選択に答えたあとGMのナレーションが続くか（友達コメントにならないか）、(c) 「うんうん」のあと5秒間隔のLLM生成がログに出ないこと、(d) ラジオが `WAIT` で止まらないこと。
