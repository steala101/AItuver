# Phase 6F: Identity 移行の運用UIと、過去記憶の人物単位検索

- 日時: 2026-08-02 20:05 JST
- 担当: Claude (Opus 5)
- 変更台帳: `docs/SHARED_CHANGELOG.md` の 2026-08-02 20:05 の項
- 状態: 自動回帰済み。**実機未検証**。新フラグ既定オフ
- 前段: `docs/handoffs/2026-08-02_1820_claude_relationship-migration-phase6e.md`

---

## 1. 追加した移行UI

**新しい管理画面は作っていない。** 既存の🩺オーバーレイに区画を1つ足した。

出しているもの:

| | |
|---|---|
| 現在のモード | 対象話者数 / 確認済みLink数 |
| 未解決 / 競合 | Shadow Read の差分軸数 |
| 部分的失敗 | 最後の移行ID / 日時 / 戻せるか |
| ジョブの状態 | 進捗・成功・競合・失敗 |
| 話者一覧 | Identity の解決状態つき |

**声紋ベクトルも音声も関係値そのものも出していない。**

Local については「**推定話者数**」と明記した。接続の一覧が無いのは
環境の制約で、確定値として出さない。

---

## 2. 許可する状態遷移

```
disabled ──→ shadow_read ──→ dual_write ──→ person_primary
                 │                │                │
                 └────────────────┴────────────────┴──→ legacy_rollback
legacy_rollback ──→ disabled / shadow_read
```

**`disabled → person_primary` と `shadow_read → person_primary` は禁止。**
差を一度も見ないまま正式採用にすると、後で食い違いに気づいても
どのターンから間違っていたのか分からない。

**UIのボタンを隠すだけにしていない。** `Mind.request_migration()` が
`can_transition()` をもう一度呼ぶ。押せなくしても別の経路から呼べば
通ってしまうので、判断はバックエンドに置いた。

---

## 3. 昇格を拒否する条件

| 上げ先 | 止める理由 |
|---|---|
| どれでも | person ストアが無い / Identity Resolver が無い |
| `dual_write` | Shadow Read が 20 回未満 / 書き込み失敗が残っている |
| `person_primary` | Dual Write が一度も走っていない |
| | **部分的失敗が残っている** |
| | Shadow Read の差がまだある |
| | 未完了の移行がある |

**戻す方向（`legacy_rollback` / `disabled`）は条件を付けていない。**
逃げ道を条件付きにすると、困った時に逃げられない。

Identity の競合は昇格を止めない——競合対象は legacy へ落ちるので、
全体を止める理由にならない。**person 側へ強制統合はしない。**

---

## 4. Migration Job の仕組み

```
MigrationJobRunner.start()
→ 既に走っていたら、それをそのまま返す（二重起動しない）
→ 別スレッドで1件ずつ
→ 進捗・成功・競合・失敗を数える
→ completed / completed_with_conflicts / failed
```

**音声の経路で同期実行しない。** 移行の間ずっと応答が止まり、
危険警告も出なくなる。会話を止めないのが先（第17条）。

UI 側でも押した瞬間にボタンを無効化する。連打で2つ目のジョブを
作らせない。UI を開き直しても `runner.status()` から取り直せる。

移行が転んでも例外は外へ出さない——`FAILED` として記録するだけ。

---

## 5. IdentityScope

```
IdentityScope:
    person_id
    primary_speaker_id       いま話している声。**検索の先頭**
    confirmed_speaker_ids    有効な CONFIRMED リンクだけ
    transport_identity_ids   Discord ID など（記憶検索には使わない）
    excluded_revoked_ids     外したもの。**何を外したかを残す**
    resolution_status
```

含めないもの: `CANDIDATE` / `CONFLICTED` / `REVOKED` / `SUPERSEDED`。
誤ったリンクで拾った記憶を、その人のものとして語り続けることになる。

**人物が解決できていなければ、今の `speaker_id` だけ。**
分からないまま範囲を広げると、別人の過去を自分のものとして語り出す。

---

## 6. 過去記憶の人物単位検索

```
ParticipantIdentityRef → IdentityScope → 確認済み speaker_id 群
→ 各IDで既存の検索 → memory_id で重複排除 → 既存のranking → 少数だけ
```

**過去のレコードは1行も書き換えていない。** 検索側で束ねるだけ。

### 予算を守る

1つあたりの読み込み上限を人数で割る（`scan_limit // len(keys)`）。
返す件数は `max_retrieved` のまま。**人数倍にしない。**

> **自分のテストが自分のバグを見つけた。** 最初は `person` モードでも
> legacy を別に読んでいて、読み込み量が予算の2倍（40+40）になっていた。
> 人物単位の結果に legacy が含まれているので、二重に読む必要は無い。

### モードは関係値と揃える

| 移行モード | 記憶の検索 |
|---|---|
| `disabled` / `legacy_rollback` | 今の `speaker_id` だけ |
| `shadow_read` / `dual_write` | 正式は legacy、**差だけ測る** |
| `person_primary` + フラグ | 人物単位を正式採用 |

関係値が legacy を読んでいるのに記憶だけ人物単位、という食い違いを
作らない。片方だけ進むと、どちらの結果を見ているのか分からなくなる。

---

## 7. Link 取消時の動作

- 以後の `IdentityScope` から**外れる**
- 過去の記憶は**物理削除しない**
- 別の人物へ**自動移動しない**
- `legacy` モードでは元の `speaker_id` から今までどおり引ける
- 何を外したかは `excluded_revoked_ids` に残る

---

## 8. speaker_status 表示の変更

`Mind.speaker_status_rows()` を足した。話者ごとに:

| 表示 | 意味 |
|---|---|
| `Legacy Speaker` | 声紋プロファイルだけ。人物へ繋がっていない |
| `Resolved Person` | 確認済みで人物が決まっている |
| `Unknown` | 候補どまり |
| `Conflicted` | 人物が割れている |
| `Revoked` | 全てのリンクが取り消されている |

**表示のために関係値も Identity も更新しない。**

---

## 9. 追加テストと結果

- `tests/test_migration_ui.py` — 58件
- 全体 **2399 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**

### 故障注入

| 切ったもの | 赤くなったもの |
|---|---|
| `disabled → person_primary` を許す | `migration.transitions` |
| 部分的失敗を無視して昇格する | `migration.transitions` |
| 取り消したリンクを検索範囲へ含める | `migration.memory_scope` |

### 自分のバグ2件

1. **読み込み量が予算の2倍**だった（上記）。
2. `migration.transitions` プローブが `IdentityResolver` を渡さずに
   検証していて、**本番の設定で常に赤**だった。

---

## 10. 実機での移行・ロールバック手順

### 準備

```yaml
identity_migration:
  ui_enabled: true      # まずは見るだけ
```

🩺を開き、区画が出ること。数字が全部 0 でも正常。

### ① DRY RUN

「DRY RUN」を押す。**何も変わらない。**
`migratable` と `blocked` を見て、何件動いて何件止まるかを確認する。

### ② SHADOW READ

```yaml
identity_migration:
  enabled: true
  mode: shadow_read
  shadow_read_enabled: true
```

普段どおり会話する。**会話は変わらないのが正しい。**
🩺の「差分軸」を見る。person 側が空なので最初は大きい。

### ③ 関係値の移行

「関係値を移行」を押す（確認ダイアログが出る）。
ジョブが走り、**その間も会話できること**を確かめる。
終わったら「差分軸」が 0 になること。

### ④ DUAL WRITE

```yaml
  mode: dual_write
  dual_write_enabled: true
```

関係値が動く会話をする。🩺の「部分的失敗」が 0 のままであること。

### ⑤ 記憶の差を測る

```yaml
memory:
  identity_scope_shadow_read_enabled: true
```

過去に触れる会話をする。**返ってくる記憶は変わらないのが正しい。**

### ⑥ PERSON PRIMARY

```yaml
  mode: person_primary
  person_primary_enabled: true
```

**部分的失敗や差分が残っていると拒否される。** それが正常。
昇格できたら、既知の相手と話して**口調・距離感が変わっていないこと**。

### ⑦ 記憶を人物単位で引く

```yaml
memory:
  identity_scope_retrieval_enabled: true
```

**いちばん確かめたいところ**:

1. Discord で「◯◯の話したよね」と過去に触れる。
2. 同じ人が**ローカルマイクから**同じ話題に触れる。
3. **同じ記憶が返ること。**
4. 返ってこないなら、声紋リンクがまだ `CONFIRMED` でない
   （3回以上の一致が要る）。🩺の話者一覧で確認する。

### ⑧ ロールバック

🩺で「従来へ戻した へ」を押す（確認ダイアログが出る）。

1. 会話が成立すること。
2. **移行前と同じ距離感**で話すこと。
3. person 側のデータは消えていない（もう一度上げれば戻る）。

---

## 11. 残っている重要な穴

- **実機未検証**（上の手順は未実行）。
- **`Mind.speaker_status()` の直読みが UI に残っている。**
  `speaker_status_rows()` を足したが、既存の呼び出し側は差し替えていない。
  表示だけなので実害は小さいが、`UNSAFE_DIRECT_REFERENCE` のまま。
- **過去の会話履歴と世界状態は人物単位にしていない**（仕様どおり）。
  記憶だけが `IdentityScope` を使う。会話履歴を横断したくなったら、
  同じ形で別途やる作業。
- **Migration Job は再起動を越えない。** 実行中にアプリごと落ちると、
  ジョブの記録は消える（移行そのものの記録は `mind.db` に残るので、
  もう一度走らせれば冪等に続きから進む）。
- **記憶の重複排除は `memory_id` だけ。** 同じ内容が別IDで2件あると
  両方残る——既存の ranking が抑えるはずだが、実機で見たい。
- Local の推定話者数の制約は Phase 6D のまま（環境の制約）。
