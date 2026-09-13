# Codex への引き継ぎ（2026-08-04 02:00 / Claude Opus 5 より）

**最初にこれを読み、次に `docs/handoffs/_NEXT_SESSION.md` を読んでください。**

---

## 0. いま何が起きているか（1分で）

Phase 7D の途中です。**ペルソナごとの記憶分離を本番有効にした直後**で、
実機のスモークテストだけが残っています。

```
②③ TurnFrame / TurnCommitLedger を実経路へ配線   完了
⑤   Memory / Reflection へ persona_id を伝播      完了
⑥   Relationship を persona_id + person_id へ     完了
    旧記憶214件の帰属移行                          完了（実データへ適用済み）
    分離の観測点                                   完了

⓪   実機スモークテスト   ← **チビの作業。まだ結果が出ていない**
①   30ターンの実測       ← ⓪の後
④   PersonaContext のターン固定
⑦   静的ペルソナキャッシュの要否判断
```

**Phase 8 へは進まないでください。** ⓪と①が終わるまで、とチビから
指示が出ています。

---

## 1. あなたが最初にやること

### まず ⓪ の結果を待つ

チビが実機でスモークテストを回します。手順は
`docs/handoffs/2026-08-04_0100_claude_legacy-scope-migration.md` §9。
結果は `logs/cognitive_trace.jsonl` の該当行として渡されます。

**結果が来る前にコードを直さないでください。** いま分離は本番有効で、
確かめる前に触ると、失敗した時に「移行の問題か・あなたの変更か」が
分からなくなります。

### スモークテストが失敗した時の対応

| 症状 | 見る所 |
|---|---|
| ポッポの202件が出ない | `memory_retrieval_trigger` が false なら検索トリガー。true で取得0件なら ranking か scope 条件。**別 persona_id が出たら重大** |
| `persona_leak: true` | 取れた記憶に他人が混ざっている。`retrieved_memory_persona_ids` を見る |
| 新規 Memory の `persona_id` が空 | `Mind._bind_persona_scope()` が呼ばれているか |

**202件が消えたように見えた場合は、会話を続けさせずに止めてください。**
アプリ停止 → `data/legacy_scope_migrations.json` と現在の DB を保存 →
`neuro_voice.mind.legacy_scope_migration.rollback()` か
`data/backups/legacy_scope/mind_neuro.db.1ed29fa931a2.bak` から復元。

### 結果が良ければ、次はこの順で

1. **`TurnLatency` を Local / Discord の実経路へ配線**
   `neuro_voice/cognition/turn_latency.py` は集計側だけで、実経路に
   繋がっていません。**ただし `neuro_voice/utils/latency.py` の
   `TurnMetrics` が既に実経路の背骨**です。新しい計測系をもう1つ
   作らず、どちらかへ寄せてください（Phase 7D 前半の判断は
   「既存を広げる」でした）。
2. **`turn_end_to_first_audio_ms` の終点を実際の再生開始にする**
   `play_start` は既に callback ベースへ直してあります（7D 前半）。
3. **`TurnCommitLedger` の実配線を実機で確認**
   配線済み・`turn_frame_enabled: true`。次に重複が出れば
   `duplicate_stage` に層が入ります。
4. **30ターンの p50 / p90 / p95**
5. **重複が再発した層の特定**
6. **静的ペルソナキャッシュの要否を実測で判断**（下の④⑦参照）

### ④ PersonaContext をターン開始時に固定する

`cognition/persona_scope.py` の `PersonaContext` に
`config_fingerprint`（人格設定の内容ハッシュ）を足し、**ターンの頭で
1回作って最後まで使い回す**。いまは各処理が `active_persona_id` を
その都度読み直すので、ターン途中で切替が起きると**前半と後半で違う
人格の設定**を見ます。プロンプトキャッシュの鍵にも使います。

### ⑦ 静的ペルソナキャッシュの要否判断

人格の静的部分（system prompt・口調・興味）の組み立て結果を使い回すか。
**`persona_context_build_ms` を実測してから決めてください。**
有意でなければ作らない。動的状態（会話履歴・感情・関係値・World State・
取得 Memory）は絶対に入れない——入れると古い状態で喋ります。

---

## 2. 触ってはいけないもの・守ること

### チビからの継続指示

- **設定のデフォルトを勝手に本番有効へ変更しない。**
  例外として 2026-08-04 に**明示指示で**有効化したのは:
  `memory.episodic_enabled` / `memory.retrieval_enabled` /
  `turn_integrity.turn_frame_enabled` / `turn_integrity.persona_scope_enabled` /
  `cognition.trace.enabled`。
  **`memory.reflection_enabled` / `turn_integrity.duplicate_suppression_enabled` /
  `turn_integrity.allow_legacy_unscoped_memory` は false のまま。**
  Reflection は Memory 分離が通ってから、テストセッションで有効化します。
- `AGENTS.md` の禁止事項（Git 操作・秘密情報の複製・
  Local/Discord 片側だけの変更・変更履歴の未更新で完了報告）。
- 憲法と衝突する提案は、黙って再設計せず
  `CONSTITUTION_AMENDMENT_PROPOSAL` として提示する。
- **報告にコード全文や巨大 diff を貼らない。**

### 実データの扱い

- `data/mind_*.db` は**本番の記憶**です。202件と12件は移行済み。
- **マウント越し／ネットワーク越しに SQLite を書かないでください。**
  私はこれで `disk I/O error` を起こし、`mind_luna.db` を一度壊しました
  （バックアップから復旧済み）。ローカルへ複製して作業し、完成した
  ファイルを書き戻す手順にしてください。
- `data/mind_luna.db-journal` が **0バイトで残っています**。害はありませんが、
  チビが Windows 側で削除する予定です。**`mind_luna.db` 本体は触らないこと。**

---

## 3. 効いたやり方（引き継ぎたい習慣）

### 故障注入を必ずやる

「実装がある」と「効いている」は違います。ガードを1つ消して、
**テストとプローブが赤くなること**を確認してください。
この習慣で、これまでに**自分のバグを12件**見つけています。

とくに、**このプロジェクトで実際に踏んだ「呼び出し側が無い」パターン**:

```
TurnFrame / TurnCommitLedger  型もテストもあるのに呼ばれていなかった
bind_persona()                定義だけあって呼び出し側が無かった
cognition.trace.enabled       観測点を足したが書き出し口が既定オフだった
```

**新しく何かを足したら、「それを呼んでいる所」を必ず数えてください。**
`tests/test_persona_write_scope.py` と
`tests/test_relationship_persona_key.py` に、**「作る箇所の数」と
「渡す／結ぶ箇所の数」を数えて比べる**テストがあります。同じ形を
真似すると、経路が増えた時に気づけます。

### 自分のテストが空振りしていないか疑う

故障注入で**私のテスト3件が空振りしていた**のが分かりました。
同じ罠にはまらないように:

1. **定数と比べない。** `assert scopes == {DEFAULT_SCOPE}` は、定数を
   書き換えるとテストも一緒に動いて素通りします。リテラルで比べる。
2. **変化しない値で「変えていない」を確かめない。** 同じ persona へ
   帰属させる移行では、上書きしても値が変わらないので検出できません。
   区別できる別の印（`scope` など）を見る。
3. **前後で同じ関数を使うだけの検証は、その関数を弱めても一致します。**
   指紋なら「指紋が実際に本文と embedding を覆っているか」を別に見る。

### プローブはソースを読むだけにしない

実際に往復させてください。読むだけの点検は故障注入をすり抜けます
（`migration.single_start` で一度踏みました）。

### 実機でしか分からないことを「問題ない」と書かない

自然さ・音質・体感速度・分離が実際に効いているか。**チビに聞いて
ください。** 私は8/3 に「分離できている」と報告しましたが、実際は
`memory.retrieval_enabled: false` で記憶が0件だっただけでした。

---

## 4. 地図（Phase 7D で私が足したもの）

```
neuro_voice/cognition/
  turn_tracker.py            TurnFrame/CommitLedger を実経路へ挿す層
                             （Local と Discord が同じ物を使う）
  trace.py                   分離の観測点（persona_leak 判定）

neuro_voice/mind/
  legacy_scope_migration.py  旧記憶の帰属移行（監査/dry-run/backup/
                             ledger/rollback）
  store.py                   持ち主とスコープを決める1箇所（_owner）
                             owners_of() / scope_rejections()
  mind.py                    _bind_persona_scope() / persona_trace_fields()
  relationship.py            関係値の持ち主（他人のファイルは読まない・
                             書かない）

tests/
  test_turn_tracker_wiring.py       27件
  test_persona_write_scope.py       13件
  test_relationship_persona_key.py  13件
  test_legacy_scope_migration.py    20件
  test_persona_create.py            35件（ペルソナ追加・削除 UI）
```

### サンドボックスでのテスト

```
rm -rf /tmp/p7 && mkdir -p /tmp/p7
cp -r neuro_voice tests config data game_profiles docs /tmp/p7/
```

`/tmp/p7/conftest.py` に Python 3.10 用の `StrEnum` shim（詳細は
`_NEXT_SESSION.md` §6）。スタブは `/tmp/stub`（sounddevice）と
`/tmp/stub2`（openai）。**セッションが変わると /tmp が消えるので
作り直しが要ります。**

```
cd /tmp/p7 && timeout 42 python3 -m pytest tests/ -q -p no:cacheprovider \
  --ignore=tests/test_style_bert_vits2.py --ignore=tests/test_ui_mind_status.py \
  --ignore=tests/test_discord_screen_share.py --ignore=tests/test_local_audio_liveness.py \
  --ignore=tests/test_remote_launcher.py --ignore=tests/test_ktane_verify.py
```

現在 **2761 passed / 17 subtests**、pyflakes clean、診断**要注意 0 件**
（110プローブ / warm 48ms）。

`tests/test_ktane_verify.py` を除外しているのは、`/tmp/ktane_state.json`
を固定パスで書くテストで、前セッションの残骸があると
`PermissionError` になるためです。**実装の問題ではありません。**

---

## 5. 終わる前に必ず

```
docs/SHARED_CHANGELOG.md          先頭へ追記
docs/handoffs/YYYY-MM-DD_HHMM_codex_<task>.md   詳細
docs/handoffs/_NEXT_SESSION.md    現在地を更新
```

**更新していない状態で「完了」と報告しない**（AGENTS.md）。
未解決リスクは `docs/KNOWN_RISKS_AND_DEBT.md` へ（直近は R-045）。

分からないこと、判断が割れたことは、黙って決めずにチビへ出してください。
