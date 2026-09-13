# Phase 6G: 移行の最終配線・障害復旧・記憶の重複排除

- 日時: 2026-08-02 21:40 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 21:40 の項
- 状態: 自動回帰済み。**実機未検証**
- 前段: `docs/handoffs/2026-08-02_2005_claude_migration-ui-phase6f.md`

---

## 1. speaker_status UI の変更

`Mind.status()` の中で `_relationships.snapshot(f"speaker:{id}")` を
**直接**読んでいた。`person_primary` にすると会話は person 側を使うのに、
**画面だけ legacy の古い値を出し続ける**——同じ相手なのに数字が違う、
という気づきにくい食い違いになる。

`_speaker_relationship_snapshot()` を通すようにした。移行の段階に応じて
legacy か person かが自動で切り替わる。あわせて話者一覧へ
`identity`（`person_id` と `Legacy Speaker` / `Resolved Person` /
`Unknown` / `Conflicted` / `Revoked`）を添えた。

**`relationship_status()` は互換のため残してある。**
表示のために関係値も Identity も更新しない（テストで固定）。

---

## 2. Migration Job の永続化

`mind.db` に `migration_jobs` 表を1つ足した。

```
job_id / operation / requested_mode / previous_mode / status
progress_cursor / processed_count / success_count
conflict_count / failure_count / process_id
created_at / started_at / updated_at / completed_at / error_summary
```

**声紋も記憶本文も関係値も入らない**（列の集合をテストで固定）。

**1件ごとに書く。** まとめて最後に書くと、落ちた時にどこまで
進んだのか分からない。

---

## 3. アプリ再起動時の復旧

```
起動 → interrupt_stale_jobs(PROCESS_ID)
     → 起動IDが違う RUNNING / PENDING → INTERRUPTED_RECOVERABLE
     → 🩺に「前回の移行が中断された」と出す
```

`PROCESS_ID` はプロセスごとの起動ID。**時刻だけで判断すると、
時間のかかる移行と落ちた移行を区別できない。** 同じプロセスの
`RUNNING` は触らない（テストで固定）。

**勝手に再開しない。** 続けるかどうかは人が決める。
起動しただけで `person_primary` へ上がることも無い。

---

## 4. Resume / Rollback の動作

### Resume

```
INTERRUPTED_RECOVERABLE → RESUME
→ progress_cursor の次から
→ 完了済みは飛ばす
→ start_kind = "resume" として記録
```

50件処理して落ちたら、51件目から。移行そのものが冪等なので
飛ばし損ねても二重適用にはならないが、**無駄に走らせない**。

### 中断中の排他

**中断を無視して新しい作業を始めない。** `resume` と
`legacy_rollback` 以外は `interrupted_job_pending` で断る。
UI も中断中はその2つしかボタンを出さない。

### Rollback

`legacy_rollback` を選ぶと、中断していた作業を `CANCELLED` にする。
**残しておくと「途中で止まっている」と言い続けて、次に進めなくなる。**
person 側のデータは消さない。

---

## 5. Memory Fingerprint

```
information_type + 正規化した summary + 正規化した topic
→ SHA-256 の先頭16文字
```

正規化は **NFKC・空白の統合・ASCII英字の小文字化・末尾句読点の統一**だけ。
語順の並べ替えも同義語寄せもしない——**そこから先は推測**で、
推測で記憶を落とすと「言ったのに覚えていない」が起きる。

**LLM を呼ばない。** 毎回モデルを使うと遅いうえに、同じ入力で
違う答えが返る。

> **自分のテストが自分のバグを見つけた。** 最初は指紋に
> `participant_ids` を入れていた。すると Discord (`speaker:3`) と
> Local (`speaker:7`) に紐づいた**同じ話が必ず別物**になり、
> 重複排除が一件も効いていなかった——**今回の目的そのものが未達**
> だった。ここへ来るのは1人分の範囲（`IdentityScope`）で集めた
> ものだけなので、別人が混ざる心配は無い。外した。

---

## 6. 重複時の代表選択

物理削除せず、1件だけ返す。優先順位:

1. **覆された・置き換えられた記憶は後ろ**
2. `USER_STATEMENT` > `SYSTEM_FACT` > `OBSERVATION` >
   `REFLECTION` > `INFERENCE`
3. confidence が高い
4. importance が高い
5. 新しく検証されている
6. memory_id（同点を決定的にするため）

**`USER_STATEMENT` と `INFERENCE` は指紋が違う**ので、そもそも
統合されない。似た文でも「本人がそう言った」と「こちらがそう推測した」
は別のことだから。

似ているだけのもの（2-gram の重なり 85% 以上・**同じ出典同士のみ**）は
`possible_near_duplicates` に印を付けるだけで、**落とさない。**

---

## 7. 追加テストと結果

- `tests/test_migration_recovery.py` — 61件
- 全体 **2458 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**

### 故障注入

| 切ったもの | 赤くなったもの |
|---|---|
| 出典を無視した指紋にする | `memory.dedup` |
| 起動しただけで再開する | `migration.recovery` |

### 自分のバグ2件

1. **指紋に `participant_ids`** を入れていて、重複排除が効いていなかった
   （§5）。
2. **点検が 291ms**（上限 250ms）になっていた。`migration.recovery` が
   毎回一時ディレクトリと SQLite ファイルを作っていた。3秒ごとに回る
   点検が会話の邪魔をしては本末転倒。in-memory DB にして 25ms へ。
   `world.persistence` も同じ形だったので一緒に直した。
   **予算を超えたら赤くなるテスト**を足してある。

---

## 8. 実機確認手順

### A. 移行

1. `identity_migration.ui_enabled: true` → 🩺に区画が出る。
2. **DRY RUN** を押す。何も変わらない。件数を見る。
3. `mode: shadow_read` + `shadow_read_enabled: true`。
   **会話が変わらないこと。**
4. Discord と Local で**同じ人**として会話する。
5. 🩺の「差分軸」を見る（person 側が空なので最初は大きい）。
6. 「関係値を移行」→ 確認 → 実行。**その間も会話できること。**
7. 「差分軸」が 0 になること。
8. `mode: dual_write` + `dual_write_enabled: true`。
   関係値が動く会話をして「部分的失敗」が 0 のまま。
9. **アプリを再起動。** 🩺で記録と関係値が残っていること。
10. `mode: person_primary` + `person_primary_enabled: true`。
    **口調・距離感が変わっていないこと。**

### B. 記憶の共有

1. `memory.identity_scope_retrieval_enabled: true`。
2. **Discord で固有の内容**を覚えさせる（例:「先週ダイヤを32個掘った」）。
3. **Local マイク**から関連する話題を出す（例:「あのダイヤどうした？」）。
4. 同じ `person_id` として過去の記憶が返ること。
5. **同じ内容が2回渡っていないこと**——🩺のトレースで
   `dedup.by_fingerprint` が 1 以上なら効いている。
6. 返ってこない場合は声紋リンクがまだ `CONFIRMED` でない
   （3回以上の一致が要る）。🩺の話者一覧で確認。

### C. 障害復旧

1. 話者が数人いる状態で「関係値を移行」を押す。
2. **走っている最中にアプリを終了**する（タスクマネージャで落としてよい）。
3. 起動し直す。
4. 🩺に「**前回の移行が中断された**」と、何件まで処理済みかが出る。
5. ボタンが「中断した移行を再開」と「従来へ戻す」の2つだけになること。
6. 「再開」→ 確認 → 実行。
7. **完了済みが二重に適用されていないこと**——関係値の数字が
   跳ね上がっていない。

### D. ロールバック

1. 🩺で「従来へ戻す」→ 確認。
2. 従来の `speaker_id` 経路で会話が成立すること。
3. **既存の関係値が維持されている**こと（移行前と同じ距離感）。
4. person 側のデータは消えていない（もう一度上げれば戻る）。

### E. 挨拶（Phase 6D の持ち越し）

`discord.cognitive_greeting_enabled: true` で、5つの場面を試す。

| 場面 | 見るところ |
|---|---|
| 初回入室 | 毎回同じ固定文になっていないか |
| 短時間の再接続 | 挨拶を連打しないか |
| 会話中の入室 | 割り込まないか（**黙るのが正解**） |
| 既知の人物 | 長すぎないか、質問を勝手に足していないか |
| 不明な人物 | 名前で呼びかけようとしていないか |

**自然さはこちらでは判断できない。** 聞いた印象を教えてほしい。

---

## 9. 対象外として残した項目

仕様どおり、以下は今回の未達に含めない。

| 項目 | 理由 |
|---|---|
| 過去 Conversation History の物理移行 | `IdentityScope` 経由で必要時に参照できる |
| 過去 World State の人物単位移行 | 現在セッション中心。過去の一時状態の統合は危険 |
| Local の authoritative 参加者一覧 | 接続一覧を取得できない環境上の制約 |
| 近似意味による自動統合 | 誤削除・誤抑制の危険。印を付けるだけに留めた |

---

## 10. 残っている重要な穴

- **実機未検証**（上の手順は未実行）。
- **`start_kind` は再開と新規しか区別していない。** `retry` と
  `rollback` は enum に無く、`operation` 側で読む形。仕様は4種類を
  求めていたので、必要なら足す。
- **重複排除は完全一致に近い正規化だけ。** 「ダイヤを32個掘った」と
  「ダイヤ32個ゲットした」は別物のまま両方渡る。近似は印だけ。
- **`possible_near_duplicates` を誰も見ていない。** Trace には出るが、
  🩺には出していない。実機で頻度を見てから決めたい。
- **Job の履歴は12件まで。** それ以上は DB には残るが、
  `runner.history` からは落ちる。
- Phase 6F から引き継ぎ: 過去の会話履歴と世界状態は人物単位にしていない。
