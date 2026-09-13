# AI作業Handoff

- 担当AI: Claude
- Role: test / implementation
- Task: Plan Adherence 初回計測の読み取りと、自作の物差しの較正
- Started At: 2026-07-29 20:05 JST
- Finished At: 2026-07-29 20:40 JST
- Project Root: `C:\Users\coala\Desktop\Grok_API\Codex\AItuber`
- Branch/Worktree: 既存作業ツリー（Git操作なし）
- Start Commit: 触れていない
- End Commit: 触れていない
- Work Lock: Codexは上限のため停止中。単独編集

## 目的

「会話のテンポと受け答えのセンスがネウロ様/Evilに届かない」という指摘に対し、
原因が (a) Plannerが毎回同じ判断をしている のか、
(b) Plannerは変えているのに本文へ届いていない のかを、印象ではなく数字で切り分ける。

ユーザーが `docs/CONVERSATION_AB_SCRIPT.md` のA〜Fをチャットへ打ち込み、
「1と5の応答内容はほぼ一緒でした」と報告。その回の
`logs/conversation_metrics.jsonl` 8ターンを読んだ。

## 調査結果

- 根本原因: **(b) 伝達の問題**。以下が根拠。
  - shape 5種類 / style 5種類、`shape_streak = 1`、`style_streak = 1` ——
    Plannerは十分に振れている。よって (a) は否定される
  - それでも **ターン1（proposal_path / playful）とターン5（react_expand /
    opinionated）の冒頭ハッシュが同じ `a4b5`**。計画がまったく違うのに
    書き出しが一致した。ユーザーの体感と一致する
  - `ask_follow_up` が 3/8 違反。`checkable_ratio_mean = 0.542`、
    `adherence_mean = 0.615`、`strict_mean = 0.333`
- 根拠ファイル/関数: `neuro_voice/dialogue/plan_metrics.py` `evaluate` /
  `DiversityWindow.metrics`、`logs/conversation_metrics.jsonl`
- state owner: 変更なし
- async/queue/cancel: 変更なし
- privacy/data: 本文は読まず、`opening_hash` / `reply_chars` のみで判断した

## 自分の計測の誤り（本作業の主題）

初回の `target_length` 6/8 違反は**信用してはいけない数字**だった。

- 実測の返答は 61〜135文字。旧帯は `short = (0,70)` / `medium = (30,190)` /
  `long = (110,∞)`。**71文字は short を1文字超え、106文字は long を4文字
  下回った**だけで「計画を無視した」と判定していた。
- さらに **計画された長さの値をログへ残していなかった**ので、
  どの違反がどの帯だったのか事後に確認すらできなかった。
- これは同じセッションでnum_ctx掃引の平均を取って嘘の数字を出したのと
  同型の失敗（測る側が壊れているのに、測られる側の欠陥として報告した）。

## 変更

| File | Change | Why |
|---|---|---|
| `neuro_voice/dialogue/plan_metrics.py` | `_LENGTH_BANDS` を `short=(0,95)` / `medium=(45,220)` / `long=(100,10000)` へ較正 | 日本語の話し言葉は1文25〜35文字。personaは1〜3文を求める。**推測ではなく文数**を根拠に帯を引き直した。境界での数文字差を違反にしない |
| `neuro_voice/dialogue/plan_metrics.py` | `TurnRecord.planned_length` / `planned_question` を追加し `snapshot()` へ出力 | 違反だけ残しても事後に読めない |
| `neuro_voice/dialogue/plan_metrics.py` | `record_for()` が計画値を記録 | 同上 |
| `tests/test_plan_metrics.py` | 実測値71/106文字が違反にならないこと、計画値が記録されること、`ask_follow_up` 未指定は `False` ではなく `None` として残ること（+5件、計54件） | 較正の根拠を回帰として残す |

## 変更しなかったもの

- 会話の判断ロジック。今回も計測のみで、LLM呼び出しは増えていない
- persona（ユーザーの創作物。触る前に承認を得る）
- `ask_follow_up` 3/8違反 と ターン1/5の冒頭衝突は**帯の較正の影響を受けない**ので、
  この2件の結論はそのまま有効

## 設計判断

- 帯を**重ねた**（short上限95 / medium下限45 / long下限100）。
  境界ちょうどに落ちるのは不服従ではない。狼少年の計測は無いより悪い。
- `planned_question` は「未指定」と「質問しない計画」を区別するため
  `bool` ではなく `bool | None`。潰すと後で読めなくなる。

## テスト

| Command/Case | Result | Evidence |
|---|---|---|
| `pytest tests/test_plan_metrics.py -q` | 54 passed | サンドボックス実行 |
| 全体回帰（`--ignore` 5件は既知の実機依存） | **983 passed / 11 subtests** | サンドボックス実行 |

- 未実施: 較正後の再計測（実機が要る）
- Manual: ユーザーがA〜Fを打ち込んだ1回分のログを読んだのみ
- Performance p50/p95: 変更なし（計測は非同期経路に入っていない）

## 設定・Migration

- Config: なし
- DB schema / Data migration: なし
- 既存の `logs/conversation_metrics.jsonl` は**旧帯で書かれている**。
  混ぜて読まないこと。再計測分から `planned_length` 列が入る

## 失敗した試行

- なし（本作業自体が前回の失敗の修正）

## 残課題

1. `docs/CONVERSATION_AB_SCRIPT.md` の8文を**もう一度**流し、`target_length` を
   信用できる数字にする
2. 任意: `dialogue.conversation_generation.enabled: false` でPlanner無効側も取る
3. **本命は item 7 ではなく伝達側**。ターン1/5の冒頭衝突は、計画が
   プロンプト内で埋もれているか、直前の位置に無いことを示唆する。
   `insert_before_user_turn` の適用位置と、計画ブロックの表現を疑う
4. `Mind.build_context` の5000tokブロック（プロンプト側に残る唯一の削減余地）

## Rollback

`plan_metrics.py` の `_LENGTH_BANDS` と `TurnRecord` の2フィールドを戻すだけ。
他モジュールへの影響はない。

## 次に触るべきファイル

- `neuro_voice/dialogue/conversation_planner.py`（計画ブロックの表現）
- `neuro_voice/pipeline.py`（`insert_before_user_turn` の適用位置）

## 触らない方がよい箇所

- `neuro_voice/memory/persona.py`（ユーザーの創作物。提案→承認の順）

## 憲法・ADRへの影響

- Constitution amendment required: なし
- ADR added/updated: なし（R-037の「テストが実行しないコード」に加えて、
  **「較正されていない物差し」も同じ危険**として次回R更新時に追記したい）
- Architecture/Codemap updated: 不要（新規モジュールなし）

## 秘密・個人情報確認

- `.env`値を記録していない: はい
- raw audio/imageを添付していない: はい
- private transcriptを複製していない: はい（ハッシュと文字数のみ）

## 追記（2026-07-29 21:10 JST）— 較正後の2回目の計測

同じ台本をもう一度流した（turn 1732〜1739。**再起動なし**、1回目の直後）。

### 決定的な事実：冒頭ハッシュ列が2回とも完全一致

| # | run1 shape/style | 冒頭 | 字 | run2 shape/style | 冒頭 | 字 |
|---|---|---|---|---|---|---|
| 1 | proposal_path/playful | a4b5 | 135 | direct_take/imaginative | a4b5 | 125 |
| 2 | prediction_reflection/direct | 2497 | 71 | react_expand/opinionated | 2497 | 69 |
| 3 | playful_twist/playful | 2f4d | 61 | proposal_path/reflective | 2f4d | 61 |
| 4 | direct_take/explanatory | 1d5e | 119 | prediction_reflection/direct | 1d5e | 168 |
| 5 | react_expand/opinionated | a4b5 | 106 | playful_twist/playful | a4b5 | 129 |
| 6 | proposal_path/reflective | a1c8 | 77 | direct_take/imaginative | a1c8 | 81 |
| 7 | prediction_reflection/direct | ab06 | 84 | react_expand/explanatory | ab06 | 74 |
| 8 | playful_twist/playful | 3cf3 | 73 | proposal_path/reflective | 3cf3 | 71 |

- **8/8の位置で冒頭ハッシュが一致**。shape も style もすべて違うのに、書き出しが同じ
- **3番は語尾ハッシュ（4c07）も文字数（61）も一致** → ほぼ同一の発話
- `llm.temperature = 0.7`、seed 指定なし。**確率的サンプリングを通してなお一致する**
- run2 は run1 の直後なので、履歴には run1 の8往復が入っている。**履歴が違っても書き出しが動かない**

→ 書き出しは実質**ユーザーの入力だけの関数**。計画は言葉に届いていない。

### 前回の主張の訂正

「Plannerは十分に振れている」は **shape / style については正しいが、
target_length と ask_follow_up については誤り**だった。

- `planned_length` は8ターンとも **`long` 固定**。`conversation_planner.py:382-388` が
  プロファイルの `interaction_policy.response_length` を見ており、
  `data/dialogue_neuro.json` の値が `long` のため毎ターン `long` に固定される。
  **shape/style と違い、ターンごとに選ばれていない**
- `planned_question` も8ターンとも `False`。うち2ターンで質問が出た（違反）
- 較正後の違反: `target_length` 5/8（計画=long ≥100字に対し 69/61/81/74/71字）、
  `ask_follow_up` 2/8、`primary_style` 1/8
- `adherence_mean 0.698` / `strict_mean 0.396` / `checkable_ratio_mean 0.563`

### 伝達が失敗する場所の見立て

- 計画ブロックは `intelligence.prompt_context` → `mind.build_context` の末尾 →
  `insert_before_user_turn` で**ユーザー発話の直前**に入っている。位置は悪くない
- `_STYLE_INSTRUCTIONS` / `_SHAPE_INSTRUCTIONS` の文面は互いに十分違う
- それでも効かない。**systemブロックだけで5000〜6000tok、`num_ctx` は 8192**。
  計画738tokは「抽象的な内部パラメータの羅列」としてその中に埋もれている。
  `follow_up=no` という二値の指示ですら25%破られている以上、
  **この規模のプロンプトでは指示そのものが従われていない**と読むのが自然

### まだ取っていない対照実験

**Planner無効側（`dialogue.conversation_generation.enabled: false`）を一度も取っていない。**
無効でも同じ8つの冒頭ハッシュが出るなら、Plannerは何も寄与しておらず、
どこを削るべきかが確定する。再起動1回と8メッセージで済む。**次はこれを先に取る。**

## 追記2（2026-07-29 21:50 JST）— 対照実験（Planner無効）の結果

`dialogue.conversation_generation.enabled: false` で同じ8文を流した（turn 1740〜1747）。

| # | Planner有効(2回とも) | Planner無効 | 一致 |
|---|---|---|---|
| 1 | a4b5 | a4b5 | ○ |
| 2 | 2497 | 2497 | ○ |
| 3 | 2f4d | 2f4d | ○ |
| 4 | 1d5e | 1d5e | ○ |
| 5 | a4b5 | a4b5 | ○ |
| 6 | a1c8 | a1c8 | ○ |
| 7 | ab06 | ab06 | ○ |
| 8 | 3cf3 | 8884 | × |

- **7/8で一致**。738tokの計画ブロックを丸ごと外しても、書き出しはほぼ変わらない
- **無効側では1番と5番が完全一致**（冒頭 `a4b5` / 語尾 `988d` / 71字）。
  1番「今のAIって話しててもAI感が強いんだよね」と
  5番「またAIが『なるほど、それは重要ですね』って言ってる」は**別の入力**である。
  それに**同一の返答**を返した。`temperature 0.7` / seed なしで
- 有効側では1番と5番の語尾・字数は違っていた（135/106字、125/129字）。
  **計画が動かしていたのは末尾だけで、入り方は動かしていなかった**

### 結論

`ConversationPlanner` は書き出しをほとんど決めていない。
**会話エンジンや名前付き行動を足す方向は、対照実験によって否定された。**

返答を決めているのは常時ONのプロンプトである。
systemブロック 5000〜6000tok に対し `num_ctx = 8192`、
`context_reserve_tokens = auto`（`max_tokens` 1024 + 余裕）なので、
**履歴に残る予算は1000tok強しかない**。
モデルは「ユーザーに」ではなく「規則の山に」応答している。

### 計測バグ（自分のもの）を1件修正

`last_plan` はPlannerを止めてもプロファイルに残る。そのため対照側の8ターンは
**存在しない計画に対する `proposal_path / reflective / long`** を記録し、
`target_length` 違反まで付けていた。対照が計画を捏造しては比較にならない。
`intelligence._record_plan_metrics` でPlanner無効時は `plan = {}` にした
（`tests/test_plan_metrics_control_arm.py` 3件）。

### ようやく手に入ったもの

**判定基準**。プロンプトを削った状態で同じ8文を流し、
`a4b5 / 2497 / 2f4d / 1d5e / a4b5 / a1c8 / ab06` が割れれば効いている。
割れなければ効いていない。印象で議論しなくてよくなった。

## 次の担当への一文

**会話エンジンを足してはいけない。** 対照実験がそれを否定した。
やるべきは常時ONのプロンプトを削ることで、
効果は上の7つの冒頭ハッシュが割れるかどうかで判定できる。
