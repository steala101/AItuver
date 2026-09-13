# Phase 6E: SpeakerRegistry から PersonIdentity への安全な移行

- 日時: 2026-08-02 18:20 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 18:20 の項
- 状態: 自動回帰済み。**実機未検証**。既定 `disabled`
- 前段: `docs/handoffs/2026-08-02_1645_claude_identity-presence-phase6d.md`
- **Phase 6 最後の作業**

---

## 1. `speaker_id` を使用していた主要箇所

限定監査の全文は `docs/audits/2026-08-02_speaker_id_usage.md`。

| 箇所 | 分類 |
|---|---|
| `RelationshipStore._states[speaker_id]` | **MIGRATE_NOW** |
| `Mind._relationship_speaker_key()` | **MIGRATE_NOW**（差し込み口） |
| `InternalStateEngine._apply_relationship` | **MIGRATE_NOW** |
| `SpeakerRegistry` | `KEEP_LEGACY`（声紋の管理層として残す） |
| 記憶・反省・会話状態・世界状態・目標 | `RESOLVE_AT_BOUNDARY` |
| TTSの話者スロット・音声経路 | `UNRELATED`（人物とは無関係） |
| `Mind.speaker_status()` の直読み | `UNSAFE_DIRECT_REFERENCE` |

**移したのは `RelationshipStore` の鍵だけ。** 全DBの一括移行はしていない。

鍵を作る場所が `_relationship_speaker_key()` の1つしかなかったので、
そこへ Resolver を挟めば、20箇所以上ある呼び出し側を書き換えずに
段階を変えられる。**書き換え忘れた場所が古い鍵のまま動き続ける**、
という気づきにくい壊れ方を避けたかった。

---

## 2. SpeakerRegistry と PersonIdentity の役割

```
SpeakerRegistry   声紋の登録と照合。**Voice Identity の管理層。**
                  削除していない。置き換えでもない。
TransportIdentity Discord ID など、接続元が保証するID
PersonIdentity    同一人物を表す正規ID。**関係値の基準。**
```

`speaker:3`（関係値の鍵）は `voice:3`（声紋リンクの値）へ対応し、
`IdentityLink` が `person_id` へ繋ぐ。

---

## 3. Relationship Read / Write Resolver

**読み書きを1箇所へ集めた**（`RelationshipResolver`）。
各モジュールが直接 `RelationshipStore` を叩いていると、段階を変えるたびに
全箇所を直すことになり、必ず1つ忘れる。

### 読み

```
ParticipantIdentityRef → mode を見る
  person_primary かつ CONFIRMED → PersonRelationship
  person_primary だが未解決     → legacy（理由: person_unresolved）
  person_primary だが競合       → legacy（理由: identity_conflicted）
  shadow_read                   → legacy を返し、差だけ測る
  それ以外                      → legacy
```

返り値は `(状態, 出どころ, 落ちた理由)`。**なぜ legacy になったかが残る。**

### 書き

```
dual_write / person_primary かつ CONFIRMED → legacy と person の両方
未解決・競合                                → legacy のみ
legacy_rollback / shadow_read / disabled    → legacy のみ
```

`event_id` で二重適用を防ぐ。**legacy は常に更新する**——移行中に既存の
記録を止めると、途中でロールバックした時に空白期間ができる。

---

## 4. 移行モード

| モード | 読み | 書き |
|---|---|---|
| `disabled` | legacy | legacy |
| `shadow_read` | legacy | legacy（差を測る） |
| `dual_write` | legacy | **両方** |
| `person_primary` | **person** | 両方 |
| `legacy_rollback` | legacy | legacy |

`identity_migration.mode` と個別フラグが食い違ったら**弱い方**を採る。
強い方へ倒すと、1つのフラグの上げ間違いで person 側が正式採用になる。
知らないモード名も `disabled` へ落ちる。

---

## 5. 一対一移行の結果

```
1 speaker_id → 1 CONFIRMED Link → 1 person_id → 競合なし
→ 既存の値を読む
→ 記録を作る（previous_data = 戻すのに要る分だけ）
→ PersonRelationship へ写す
→ 移行済みの印
→ **既存は消さない**
```

冪等。同じ移行を2回走らせても記録は1件、値も二重に入らない。
`migrate_all` の `applied` は**その呼び出しで実際に動いた分**だけ。

---

## 6. 競合時の処理

### 複数の speaker_id が同じ人物

| 状況 | 判定 |
|---|---|
| 片方が未使用（`untouched`） | `safe` — そのまま写す |
| 値が同じ | `safe` |
| 差が `MERGE_TOLERANCE`(0.12) 以内 | `candidate` — 人が見てから |
| それ以上 | **`conflict` — 自動では決めない** |

**平均しない。** 信頼 .9 の人と .3 の人を混ぜた .6 は、
どちらの履歴とも合わない。

### 1つの speaker_id が複数の人物候補

自動移行しない。**一度でも `CONFLICTED` になった識別子は対象外。**
争った末に片方が消えると「候補は1人」に見えるが、関係値を写すのは
取り返しがつかない。

どちらの場合も **legacy で読める**ので会話は止まらない。

---

## 7. リンクの取り消し・繋ぎ直し

| 操作 | 起きること |
|---|---|
| `CONFIRMED` → `REVOKED` | 新しい更新は person へ行かない（legacy へ落ちる） |
| | 移した値は**残す**。記録に「要再評価」の印 |
| 別人物へ再リンク | 旧リンクは `SUPERSEDED`、新リンクは `CANDIDATE` |
| | **新しい更新だけ**が新しい人物へ |
| | **過去の値は自動で移動しない** |

過去データの再帰属は、別の明示的な移行として扱う。

---

## 8. 永続化と再起動

| 何 | どこ |
|---|---|
| PersonRelationship | `person_relationships_<persona>.json`（既存とは別） |
| IdentityMigrationRecord | `mind.db` の `identity_migrations` |
| Identity Link / Person | `mind.db`（Phase 6D） |
| migration mode | `config.yaml` |

再起動後は `hydrate()` が記録を戻し、**完了済みをもう一度走らせない**。
途中のものは `unfinished()` で見える。
記録の読み書きに失敗しても移行そのものは進む（第17条）。

---

## 9. ロールバック方法

### 全体を戻す

```yaml
identity_migration:
  mode: legacy_rollback
```

読む先が legacy へ戻り、person 側への書き込みが止まる。
**person のデータを消す必要は無い。**

### 1件だけ戻す

`IdentityMigration.rollback(migration_id)` で、その人物の関係値を
`previous_data` へ戻す。**記録は消さない**（`ROLLED_BACK` の印が付く）。

---

## 10. 追加テストと結果

- `tests/test_relationship_migration.py` — 75件
- 全体 **2340 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**

### 故障注入（赤くなることの確認）

| 切ったもの | 赤くなったもの |
|---|---|
| 食い違う値を平均する | `migration.no_average` |
| 移行記録を物理削除する | `migration.reversible` |
| 候補のリンクでも移行する | `migration.link_only` |
| 未解決でも person 側を読む | `migration.fallback` |

### **自分のプローブが自分のバグを3件見つけた**

1. **既定値を「食い違い」と誤判定していた。** 新しい `RelationshipState` は
   信頼 .5 などの初期値を持つので、値の比較だけでは移行元と違って見える。
   一対一の移行が1件目から通らなかった。`untouched()` を足して修正。
2. **争った識別子でも移行していた。** 片方が `CONFLICTED` になると
   残りは1件なので「候補は1人」に見えた。履歴に競合があれば止めるようにした。
3. **移行が値を1つも書けていなかった。** `RelationshipStore.get()` は
   写しを返すので、返り値を書き換えても保存されない。
   **この作りは正しい**（呼び出し側が共有状態を壊さないため）ので変えず、
   移行専用の `import_state()` を別に用意した。

3件目がいちばん危なかった。**テストは通っていた**——テストも同じ写しを
見ていたから。プローブが実際のストアを読んだので露見した。

### 4件目: 書きながら見つけた「呼ぶ側が無い」

この文書の「残っている穴」に
**「`attach_identity_runtime()` を pipeline / Discord から呼んでいない」**
と書いた時点で気づいた。Phase 6 で何度も踏んだ形をまた作っていた。
その場で pipeline と Discord の `initiative()` へ1行ずつ足し、
**呼ぶ側があることをテストで固定**した
（`test_both_sides_attach_the_identity_runtime`）。

---

## 11. 実機確認手順（**この順で、1つずつ**）

各段で管理者メニュー🩺の `migration` を開く。

### ① `shadow_read`（差を測るだけ）

```yaml
identity_migration:
  enabled: true
  mode: shadow_read
  shadow_read_enabled: true
```

1. 既知の相手と何ターンか会話する。
2. 🩺で `read_source` が `legacy` のままであること。
3. `shadow_difference` の軸数を見る。**最初は大きくて正常**
   （person 側がまだ空なので）。
4. **会話の内容が変わっていないこと。**

### ② 一対一の移行

1. `identity_migration()` から `migrate_all()` を実行（または UI から）。
2. 🩺で `by_status` に `applied` が増える。
3. もう一度実行 → **件数が増えないこと。**
4. `shadow_difference` の軸数が 0 になること。

### ③ `dual_write`

```yaml
  mode: dual_write
  dual_write_enabled: true
```

1. 関係値が動く会話をする（褒める・謝る等）。
2. 🩺で `write_targets` が `["legacy", "person"]`。
3. `person_write` が `applied`。**`failed` が出ていないこと。**
4. `shadow_difference` が 0 のまま保たれること。

### ④ 再起動

1. アプリを再起動。
2. 🩺で移行記録が残っていること。
3. **同じ移行が走り直していないこと**（件数が変わらない）。

### ⑤ `person_primary`

```yaml
  mode: person_primary
  person_primary_enabled: true
```

1. 既知の相手と話す → 🩺で `read_source` が `person`。
2. **会話の口調・距離感が②の前と変わっていないこと。**
3. 未登録の相手が話す → `read_source` が `legacy`、
   `fallback` に `person_unresolved`。
4. **その相手の発話で、既知の人の関係値が動いていないこと。**

### ⑥ Discord と Local の同一人物

1. 既知の Discord ユーザーが発話 → 🩺で `person_id` を控える。
2. **同じ人**がローカルマイクから発話 → 同じ `person_id` になること。
3. ならない場合は声紋リンクがまだ `CANDIDATE`
   （3回以上の一致が要る）。**それ自体は正常。**

### ⑦ ロールバック

```yaml
  mode: legacy_rollback
```

1. 会話が成立すること。
2. 🩺で `read_source` が `legacy` に戻ること。
3. **移行前と同じ距離感で話すこと。**

---

## 12. 挨拶の実機評価（Phase 6D の持ち越し）

`discord.cognitive_greeting_enabled` を上げた状態で、5つの場面を試す。
**自然さはこちらでは判断できない**ので、聞いた印象を教えてほしい。

| 場面 | 見るところ |
|---|---|
| 初回入室 | 毎回同じ固定文になっていないか |
| 短時間の再接続 | 挨拶を連打しないか |
| 会話中の入室 | 割り込まないか（黙るのが正解） |
| 既知の人物 | 長すぎないか、質問を勝手に足していないか |
| 不明な人物 | 名前で呼びかけようとしていないか |

`REMAIN_SILENT` が選ばれるのは**失敗ではない**。

---

## 13. 残っている重要な穴

- **実機未検証**（上の手順は未実行）。
- **UI からの移行操作が無い。** 今は `Mind.identity_migration()` を
  コードから呼ぶしかない。🩺は状態を見るだけ。
- **過去の記憶・会話状態・世界状態は移していない。** 仕様どおりだが、
  `person_primary` にしても記憶は `speaker_id` で引かれる。
  読む時に Resolver を挟むのは別の作業。
- **`Mind.speaker_status()` などの直読みが残っている。**
  UI 表示なので実害は小さいが、`UNSAFE_DIRECT_REFERENCE` のまま。
- Local 側に接続の一覧が無い制約は Phase 6D のまま（環境の制約）。
