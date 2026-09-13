# 所有者不明の記憶の移行と、人格分離の観測点

- 日時: 2026-08-04 01:00 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-04 01:00 の項
- 状態: completed（実データへ適用済み・**スモークテストは実機待ち**）

---

## 1. 移行前の件数

| DB | 設定上の持ち主 | memories | persona_id 空 | 判定 |
|---|---|---|---|---|
| `mind_a.db` | **設定に無い** | 0 | 0 | **BLOCKED（移行せず）** |
| `mind_b.db` | b | 0 | 0 | SKIPPED |
| `mind_c.db` | c | 0 | 0 | SKIPPED |
| `mind_luna.db` | luna | 12 | 12（列自体が無い） | COMPLETED |
| `mind_momo.db` | momo | 0 | 0 | SKIPPED |
| `mind_neru.db` | neru | 0 | 0 | SKIPPED |
| `mind_neuro.db` | neuro | 202 | 202 | COMPLETED |

Reflection は全 DB で 0 件。移行対象なし。

**`mind_a.db` を移行していない**のは、ペルソナ `a` が既に設定から
削除されているため。DB 名から `a` と読めるが、それは「名前から明らか」
ではなく「誰のか分からない」。推測で帰属させない。

## 2. DB とペルソナの対応

`config.yaml` の `persona.presets` のキーと、`mind_<key>.db` の
`<key>` を**照合してから**持ち主を決めている。ファイル名だけでは
決めない。設定に無いキーは BLOCKED。

現在の presets: `neuro / luna / neru / momo / b / c`。

## 3〜4. 移行結果

```
mind_neuro.db  202件 → persona_id=neuro  scope=persona_private
mind_luna.db    12件 → 列追加後 persona_id=luna  scope=persona_private
```

検証（両方とも通過）:

- 総数が移行前後で同じ（202/202、12/12）
- `persona_id` 空が 0 件
- 別ペルソナを設定した件数 0
- 本文・embedding の指紋が同一
- `PRAGMA integrity_check` = ok
- スコープは **`persona_private`**。共有へは上げていない

バックアップ: `data/backups/legacy_scope/`
履歴: `data/legacy_scope_migrations.json`

### 途中で起きたこと（重要）

**最初の実行が `disk I/O error` で失敗した。** サンドボックスから
マウント越しに SQLite を書いたところ、`mind_luna.db` の列追加中に
落ち、ジャーナルが残って DB が開けなくなった。

**バックアップは書き込みの前に取ってあったので、そこから復旧した**
（12件・integrity ok を確認済み）。その後は**ローカルへ複製して移行し、
完成した DB を書き戻す**手順に変えて完了させた。

* **残骸**: `data/mind_luna.db-journal` が **0バイトで残っている**。
  サンドボックスからは削除できなかった。0バイトのジャーナルは
  SQLite から見て「無い」のと同じなので害は無いが、**Windows 側で
  消しておいてほしい。**
* 教訓として: マウント越しの SQLite 書き込みは避ける。

## 5. Reflection

全 DB で 0 件だったため移行対象なし。列（`persona_id` / `scope`）は
Phase 7D ⑤ で追加済みで、これから作られる仮説には持ち主が付く。
**異なるペルソナの記憶を元にした仮説の自動移行は実装していない**
（そういうデータが無いため。必要になったら元記憶の持ち主を確認する
形で足す）。

## 6. 冪等性とロールバック

* 再実行 → `SKIPPED`（空の行が無い）
* 途中で止まった後に足された行 → 次の実行で拾う
* 別ペルソナの行がある → `CONFLICT` で停止、**何も書かない**
* 実行直前にもう一度 foreign を数える（監査から実行までの間に
  増えている可能性があるため）
* ロールバックは**このジョブが空から設定した行だけ**。移行後に
  別処理が持ち主を変えた行は触らない——「戻す」つもりで新しい
  正しい値を消すのがいちばんまずい

## 7. 追加した観測点

`CognitiveTrace` へ（**本文は入れない。ID・件数・理由だけ**）:

```
active_persona_id / active_persona_version / persona_epoch
retrieved_memory_persona_ids / retrieved_memory_scopes
cross_persona_rejection_count / cross_persona_rejection_persona_ids
legacy_unscoped_rejection_count
reflection_persona_ids / source_memory_persona_ids
persona_switch_state / _from / _to / _epoch
```

`persona_switch_state` は既存の `SwitchState` の値をそのまま出す
（正常完了は `switch_completed`）。新しい状態名は作っていない。

`trace.persona_leak` が真なら、**取れた記憶に他人が混ざっている**。

### 遅くしないための作り

* 取れた記憶の持ち主 → `id IN (...)` の**索引引き1回**
* 弾いた件数 → **集計1回**（`GROUP BY persona_id`）。
  `persona_scope_enabled` が false の時は実行しない
* どちらも本文を読まない

## 8. 設定（移行完了後に変更）

```yaml
memory:
  episodic_enabled: true
  retrieval_enabled: true
  reflection_enabled: false      # Memory 分離が通ってから

turn_integrity:
  turn_frame_enabled: true
  persona_scope_enabled: true
  duplicate_suppression_enabled: false   # 層が分かるまで false
  allow_legacy_unscoped_memory: false    # 上げない

cognition:
  trace:
    enabled: true      # ← これが無いと観測点はどこにも残らない
```

**`cognition.trace.enabled` を見落とすところだった。** 観測点を
`CognitiveTrace` へ足しても、書き出し口が既定オフでは
`logs/cognitive_trace.jsonl` に何も出ない。スモークテストを丸ごと
回してから「何も見えない」になる形だった——⑤で `bind_persona()` の
呼び出し側が無かったのと同じ穴。

**スモークテストの間だけ true にして戻す運用にはしていない。**
移行が終わっているので、このまま本番で維持できる。

## 9. スモークテスト（チビへ・実機）

### A/B

```
Aを選択 → 🩺 で active_persona_id=A を確認
「Aだけの計測用情報として覚えて。合言葉は青い灯台」など3件
→ memories が3件・persona_id=A・scope=persona_private
→ persona_id 空の新規 Memory が 0 件

Aで質問 → retrieved_memory_persona_ids が A のみ
Bへ切替 → persona_switch_state=switch_completed
        → AのMemory取得 0件 / cross_persona_rejection_count を確認
Aへ戻す → 再び取得できる
```

### ポッポ本体

`persona_scope_enabled=true` の状態で、**既存202件のうち知っている
内容を1〜2件**聞いてみてほしい。移行が正しければ引き続き出る。
**出なければ移行の問題なので、すぐ知らせてほしい**——
`data/backups/legacy_scope/mind_neuro.db.1ed29fa931a2.bak` から戻せる。

## 10. 自動テスト

- `tests/test_legacy_scope_migration.py`（新規, 20件）
- 全体 **2761 passed / 17 subtests**、pyflakes clean
- 診断 **要注意 0 件**（110プローブ / warm 48ms）

### 故障注入で見つけた自分のバグ（3件）

いずれも**テストが空振りしていた**もの。injection を入れても緑のままで
気づいた。

1. `DEFAULT_SCOPE` と比べていたので、定数を共有スコープへ書き換えると
   テストも一緒に動いて素通りした → リテラルで比べる形へ
2. 「既存の持ち主を上書きしない」を持ち主だけで見ていた。同じペルソナへ
   帰属させる移行では上書きしても値が変わらない → `scope` を印にする
3. 指紋の検証が前後で同じ関数を使うだけだったので、指紋を弱めても一致
   した → 指紋が本文と embedding を実際に覆っているかを別に見る

修正後は7種の故障注入すべてで赤くなる。

## 11. 残っている Phase 7D 項目（番号ではなく項目名で）

### ④ PersonaContext をターン開始時に固定する

`persona_scope.PersonaContext` に `config_fingerprint`（人格設定の
内容ハッシュ）を足し、**ターンの頭で1回作って最後まで使い回す**。
いまは各処理が `active_persona_id` をその都度読み直しているので、
ターンの途中で切替が起きると**前半と後半で違う人格の設定**を見る。
プロンプトキャッシュの鍵にも使う。

### ⑦ ペルソナ静的設定キャッシュの要否判断

人格の静的部分（system prompt・口調・興味）の組み立て結果を使い回すか。
**`persona_context_build_ms` を実測してから決める。** 有意でなければ
作らない。動的状態（会話履歴・感情・関係値・World State・取得Memory）は
絶対に入れない——入れると古い状態で喋る。

### そのほか


```
① 実測（30ターン p50/p90/p95）      ← スモークテスト成功後
   TurnLatency の実経路配線
   turn_end_to_first_audio_ms の実測
   TurnCommitLedger の実配線確認（重複が出た層の特定）
④ PersonaContext のターン固定
⑦ ペルソナ静的設定キャッシュの要否判断
```

**Phase 8 へは進まない。**
