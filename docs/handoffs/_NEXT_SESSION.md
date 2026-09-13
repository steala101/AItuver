# 次のセッションへ

## 2026-09-11 00:05 更新: Search evidence / follow-up / TTS UI regression fix

[最新handoff](2026-09-10_2345_sol-search-evidence-ui-fix.md)を最初に読む。変更前Local PROD 2turnでWikipedia nav反復、cached ENDINGのmode無視、TTS 422全文URL露出、system chip選択不可を観測。article-main抽出、nav boilerplate棄却、完全なBRIEF、現在modeの支持付きENDING、一回実行、安全なTTS診断、`.sys`選択/折返しを共有Local/Discord境界へ修正した。Astra reviewのImportant 4件（Discord TTS漏洩、void/malformed stack、cross-result ENDING、meta cue）も追加7 RED→GREENで修正済み。新規合計19 passed、関連447 passed / 1 expected skip、全suite3097 passed / 2 expected Windows symlink skips。次はユーザー操作Localで同じ2発話だけを再受入し、外部検索一回、本文短答、支持された結末または正直な不足、TTS/UI source/errorを確認する。browserでsource/error選択・copy・overflowも確認し、通るまでP3/Discordへ進まない。

## 2026-09-10 08:48 更新: Stage M production final

[最終handoff](2026-09-10_0848_astra-stage-m-production-final.md)を最初に読む。本番7 DBはユーザー承認済みbackup・隔離copy検証後にgeneration 8 schema/sidecarへ昇格済みで、Memory 232件・transcript 1,142件の件数/本文/summary/embedding指紋は不変。容量20%警告は共有Mind statusと「こころ」へ配線し、UNKNOWN null表示と未丸め境界を独立レビュー修正済み。全suite3078 passed / 2 expected Windows symlink skips、再レビュー指摘0。次はLocalで`饅頭こわいを検索して、短く教えて`→`さっきの話の結末も分かる？`の2発話だけをユーザー操作で確認し、通るまでP3/Discordへ進まない。

## 2026-09-10 08:18 更新: Stage M production promotion＋容量警告 close

[最新handoff](2026-09-10_0818_sol-memory-capacity-warning.md)を最初に読む。ユーザー承認後、本番7 DBは`data/backups/memory_retention_stage_m/20260910_074937_preindex`へonline backupし、integrity・件数・ID/本文/summary/embedding指紋一致と隔離copy検証後にschema/generation 8 sidecarへ昇格済み。canonical指紋不変、sidecar合計2,797,568 bytes、restart rebuild 0、既知Recall成功。共有`storage_health.capacity`はcanonical/data/backup/sidecar所在volumeを測定し、既定20% warning、probe失敗UNKNOWNをLocal/Discord/「こころ」へ同じ意味で出す。低容量でも自動削除/write停止なし。全suite3076 passed / 2 expected Windows symlink skips。永続config/`memory.write_enabled`は不変。残存は自然言語削除UX、本番実分布のfull-tier速度、長時間build lock、同権限writerによる検出不能omission、会話実機受入。

## 2026-09-10 02:10 更新: Sol Stage M v6 single-normalization close

[v6 handoff](2026-09-10_0210_sol-memory-stage-m-single-normalization.md)を最初に読む。canonical raw→unit float32を一度だけ正規化し、sidecar exact/bandsとprovenance検証を同一表現へ統一。seed55の`N(v) != N(N(v))`でもMemory/transcript連続exact Recallはrepair 0、source不変。100k＋100kはfast(.99985) Mind p95 10.374ms、full(.90) Mind p95 367.410ms、rebuild0/0、build208.847s、sidecar約320.6MB。全suite 3072 passed / 2 expected Windows symlink skips。同権限writerの検出不能omission、本番実分布のfull速度、build lock、自然言語削除UX、本番DB/実機/永続設定は残存。

## 2026-09-09 22:02 更新: Sol Stage M v5 provenance/atomic-repair close

[v5 handoff](2026-09-09_2202_sol-memory-stage-m-provenance-atomic-repair.md)を最初に読む。well-formed wrong-vectorのsidecar provenanceをcanonical bucket/exact hash再導出で検出し、close→unlink→rebuildを単一Store lock ownerへ収束。PermissionErrorはsafe fallback。100k＋100kはfast(.99985) Mind p95 12.419ms、full(.90) Mind p95 392.688ms、rebuild0/0、build214.969s、sidecar約320.6MB。全suite 3070 passed / 2 expected Windows symlink skips。同権限writerによる検出不能omission、本番実分布のfull速度、build lock、自然言語削除UX、本番DB/実機/永続設定は残存。

## 2026-09-09 21:20 更新: Sol Stage M v4 logical-poison close

[v4 handoff](2026-09-09_2120_sol-memory-stage-m-logical-poison-close.md)を最初に読む。sidecar IDとcanonical hydrateの不一致を一度だけ派生index再構築→同query再試行へ収束し、invalid候補がMemory80/transcript40を占有する場合とinvalid-only exactがfull ANNを隠す場合を両sourceで復旧。100k＋100kはfast(.99985) Mind p95 11.343ms、full(.90) Mind p95 225.459ms、rebuild0/0、build199.084s、sidecar約320.6MB。全suite 3063 passed / 2 expected Windows symlink skips。full tierの品質/速度tradeoff、build lock、自然言語削除UX、本番DB/実機/永続設定は残存。

## 2026-09-09 09:21 更新: Sol Stage M v3 final tuning

[v3 handoff](2026-09-09_0921_sol-memory-stage-m-final-tuning.md)を最初に読む。ANN品質をfixed1000組で.72以上95%へ上げ、cosine .90をMemory/transcript実働経路で復旧。不正canonical vectorを除外し、benchmark rounds注記も修正した。各100k＋100kでMind semantic p95 8.229ms、rebuild0/0だが、14表sidecar buildは194.973s・約320.6MBで同一source lockを保持する既知Minor。最終全suite 3059 passed / 2 expected Windows symlink skips。P3以降、本番DB/実機/永続設定、自然言語削除UXは未実施。

## 2026-09-09 03:40 更新: Sol Stage M final review close

[final-review handoff](2026-09-09_0340_sol-memory-stage-m-final-review.md)を最初に読む。dense deterministic ANNをMemory/transcriptの実働経路へ配線し、起動時全embedding preloadも除去。高cosine境界・旧bucket 300衝突後方、VIEW sidecar一回置換、query-only current/old schema、post-open/pre-DDL file identityまで`CODE_VERIFIED / PRODUCTION_DATA_UNTOUCHED`。各100kのMemory/transcriptで派生buildは65.934s、実働Mind semantic p95は11.576ms、再構築0/0。最終全suite 3055 passed / 2 expected Windows symlink skips。P3以降、本番DB移行、永続`memory.write_enabled`変更、実機起動は未実施。確認付き自然言語削除caller、容量/backup監視、全文transcript policy、悪意ある同directory同時writerへの完全防御は残存。

## 2026-09-09 02:41 更新: Sol Stage M review close

[review-fix handoff](2026-09-09_0241_sol-memory-stage-m-review-fix.md)を最初に読む。write-disabled retrieval、全canonical source mutationの共通失敗境界、index対象列だけの永続revision、bounded semantic candidate、canonical persona/status再検査、不正schema一回修復、sidecar link/path safetyまで`CODE_VERIFIED / PRODUCTION_DATA_UNTOUCHED`。最終全suite 3047 passed / 2 expected Windows symlink skips。P3以降、本番DB移行、永続`memory.write_enabled`変更、実機起動は未実施。確認付き自然言語削除callerも未実装で、破壊操作は別承認事項。

## 2026-09-08 21:37 更新: Sol Stage M code close

[Stage M handoff](2026-09-08_2137_sol-memory-retention-stage-m.md)を最初に読む。canonical Memory/transcriptの自動削除停止、bounded/rebuildable FTS5 sidecar、正本persona ACL再検査、実`SQLITE_FULL`のread-only退避、restart/index freshness、明示削除/DO_NOT_STORE、1k/10k/100k隔離benchmarkまで`CODE_VERIFIED / PRODUCTION_DATA_UNTOUCHED`。最終全suite 3033 passed / 1 expected Windows symlink skip。P3以降、本番DB移行、永続`memory.write_enabled`変更、実機起動は未実施。次はユーザーが戻った後、transcript全文保持範囲とbackup/disk監視を確認し、本番データ操作は別承認にする。

## 2026-09-08 08:45 更新: Sol P0〜P2 code close

[P0〜P2 handoff](2026-09-08_0845_sol_conversation-quality-p0-p2-close.md)は検索経路の履歴。共有7秒/短い設定値deadlineに加え、検索中staleの最大50ms検出、待機早期解除、query後・page開始前・redirect前の後続I/O停止まで`CODE_VERIFIED`。Mは上の2026-09-08 21:37節でcode close済み。P3以降と検索実機受入は未着手。

## 2026-09-07 22:04 更新: Sol P0〜P2実装

この節は2026-09-08の完了handoffにより更新済み。旧[P0〜P2 handoff](2026-09-07_2204_sol_conversation-quality-p0-p2.md) と [監査](../audits/2026-09-07_conversation-quality-p0-p2.md) は経緯確認用。複合検索route、query-aware Evidence、offline評価、単一deadline/cancelは`CODE_VERIFIED / HARDWARE_PENDING`。P3以降やMを同diffへ混ぜず、本番DB/Memory writeは独立Mの非削除契約まで禁止。

## 2026-09-07 更新: 会話品質の再レビュー／Solへ

10:04追記: ユーザー方針は **Local→Discord複数人、ほどほど活発・からかい多め・製作者へ強め、速度優先、Local LLM限定、現音声維持、記憶保持** に確定。[追記handoff](2026-09-07_1004_codex_quality-preferences.md)を読む。指示書末尾の回答済み質問は更新済み。P0で保持監査、独立Mで非削除契約を検証してから本番DB起動試験/Memory write再開へ進む。

最新の作業は**計画作成のみ**。まず [Sol向け指示書](../superpowers/plans/2026-09-07-conversation-quality-sol.md)、[設計根拠](../adr/ADR-0005-conversation-quality-recovery.md)、[handoff](2026-09-07_0857_codex_conversation-quality-sol-plan.md) を読む。実装担当はユーザー指定のSol。初回範囲はP0〜P2。

複合検索依頼の判定漏れと検索Evidenceの制約を再現確認。DAVE音楽・新入力切替・退出はユーザー報告で改善、検索説明は未合格。アプリコード・設定・DBは今回未変更。確認事項は指示書末尾にあり、未回答のまま本番機能やモデルを拡大しない。

以下は**2026-08-04時点の過去引継ぎ**。Phase・テスト環境・実機受入の現在値として使わず、最新の台帳・実コード・ユーザー結果で照合する。過去の本文は履歴として保持する。

---

## 過去引継ぎ（2026-08-04 02:00 更新）

**このファイルを最初に読んでください。** 新しいセッションや別のAIが
作業を引き継ぐ入口です。ファイル名の先頭が `_` なので、
`docs/handoffs/` を時系列で並べても先頭に来ます。

---

## 0. 最初にやること

0. **Codex が引き継ぐ場合は `docs/handoffs/_FOR_CODEX.md` を先に読む**
   （守ること・踏んだ罠・次の作業順が1枚にまとまっています）
1. `AGENTS.md` と `CLAUDE.md`（作業手順の最上位）
2. `docs/SHARED_CHANGELOG.md` の**最新3項**
3. このファイル
4. 直近の handoff:
   `docs/handoffs/2026-08-04_0100_claude_legacy-scope-migration.md`
   （担当交代: 2026-08-04 以降は Codex）
   （その前: `docs/handoffs/2026-08-04_0010_claude_relationship-persona-key.md`）
   （その前: `docs/handoffs/2026-08-03_2320_claude_persona-write-scope.md`）
   （その前: `docs/handoffs/2026-08-03_1120_claude_turn-tracker-phase7d-wiring.md`）
   （その前段: `docs/handoffs/2026-08-03_0740_claude_turn-path-phase7d-partial.md`）

**変更したら、応答を終える前に必ず `docs/SHARED_CHANGELOG.md` と
`docs/handoffs/` を更新する。** 更新していない状態で「完了」と
報告しない（AGENTS.md）。

---

## 1. いまどこにいるか

```
Phase 6  完了（認知カーネル・世界状態・Identity・関係値移行）
Phase 7  完了（道具の門。既定オフ・実機未検証）
Phase 7B 完了（ツール結果を会話へ。既定オフ・実機未検証）
Phase 7C 部分完了（重複/レイテンシ/ペルソナの診断基盤）
Phase 7D **部分完了。ここが現在地**
         前半（計測終点の修正）＋ ②③（TurnFrame/CommitLedger の配線）済
         **①④⑦が残り**（②③⑤⑥は完了）
Phase 8  未着手。**7D の残りを閉じるまで進まない**
```

### 直近の到達点

- 全体 **2761 passed / 17 subtests**、pyflakes clean
- **分離は本番有効**（`persona_scope_enabled: true`）。旧記憶 214件は移行済み
- **`cognition.trace.enabled: true`**。これが false だと観測点はどこにも残らない
- 診断（🩺）**要注意 0 件**、warm sweep 約47ms / **110プローブ**
- サンドボックスのテスト手順は §6

> **テスト数が前回より減って見えるのは、環境で回せない
> `tests/test_ktane_verify.py`（55件）を除外しているため。**
> あれは `/tmp/ktane_state.json` を固定パスで書くテストで、前のセッションが
> 残したファイルの所有者が違うと `PermissionError` になる。実装の問題ではない。

---

## 2. Phase 7D の残り（次にやること・優先順）

**この順番に意味がある。** 計測が正しくなったので、実測が入口。

### ⓪ A/B スモークテスト（チビの作業。**実測より先**）

`docs/handoffs/2026-08-04_0100_claude_legacy-scope-migration.md` §9。
分離が実機で成立することを確かめてから計測へ進む。
**ポッポ本体で既存202件が引き続き出るかも必ず見る**——出なければ
移行の問題で、`data/backups/legacy_scope/` から戻せる。

### ① 実測（チビの作業。**⓪の後**）

`docs/handoffs/2026-08-03_0740_claude_turn-path-phase7d-partial.md`
の §6 に手順がある。ウォームアップ3ターン → 同じ短文30ターン →
🩺 で `合計` の p50/p90/p95 と、**`TTS生成` / `再生待ち` の分離**を見る。

**数字が出るまで、レイテンシは推測で直さない**（Phase 7D 第8項）。

> **重要な前提**: Phase 7D で**測り方の誤りを2箇所直した**ので、
> 🩺 の数字は以前より長く（正しく）出る。**旧値と直接比較しない。**
> 「旧水準 約3000ms」は誤った終点で測られた値だった可能性がある。

### ② TurnFrame を実経路で作る — **完了（2026-08-03 11:20）**

`neuro_voice/cognition/turn_tracker.py` の `TurnTracker`。Local/Discord の
`speech_end` 地点で1件作り、`TurnMetrics.turn_id` を正本にする。冪等。

### ③ TurnCommitLedger を発話経路へ挿す — **完了（2026-08-03 11:20）**

発話要求・TTSジョブ・再生の3層へ挿した。詳細は
`docs/handoffs/2026-08-03_1120_claude_turn-tracker-phase7d-wiring.md` §2。

**`turn_frame_enabled` は 2026-08-04 に true にした**（記録のみ・
発話の挙動は変わらない）。次に重複が出たら層が記録される。
`duplicate_suppression_enabled` は**層が分かるまで false のまま**。

### ④ PersonaContext をターン開始時に固定

`persona_scope.PersonaContext` に `config_fingerprint` を足し、
ターン内で使い回す。各処理が `active_persona_id` を後から直読みする
のを減らす。

### ⑤ Memory / Reflection へ persona_id を伝播 — **完了（2026-08-03 23:20）**

持ち主を決める場所を `MemoryStore` の1箇所（`_owner()`）に寄せ、
`add()` / `add_episode()` / `save_reflection()` が全部そこを通るように
した。`Mind._bind_persona_scope()` を、Store を作り直す2箇所（初期化・
ペルソナ切替）の両方から呼ぶ。

**`bind_persona()` は Phase 7C で作られていたが、呼び出し側が無かった。**
読み側の絞り込みも空のまま効いていなかった。実機ログで発覚。

**持ち主はフラグに関係なく必ず付ける。** 空のまま溜めると、あとで
分離を有効にした瞬間に全部隠れる。フラグで変わるのは「持ち主不明の
私的記憶を拒否するか」だけ。

詳細: `docs/handoffs/2026-08-03_2320_claude_persona-write-scope.md`

**既存行の移行も 2026-08-04 01:00 に完了**（neuro 202件 / luna 12件）。
`neuro_voice/mind/legacy_scope_migration.py`。`mind_a.db` は設定に無い
ペルソナのため意図的に BLOCKED。

### ⑥ Relationship を `persona_id + person_id` 複合キーへ — **完了（2026-08-04 00:10）**

ファイル名では分かれていたが、**中身に持ち主が書かれていなかった**。
ヘッダへ `persona_id` を持たせ、他ペルソナのファイルは**読まないし
書かない**（読まないだけにすると、次の `save()` が相手の関係値を
空で上書きする）。持ち主の無い旧ファイルは、そのペルソナが引き取る
——冪等・取り消し可・互換 fallback はフラグ付き（既定 false）。

詳細: `docs/handoffs/2026-08-04_0010_claude_relationship-persona-key.md`

### ⑦ ペルソナ静的設定キャッシュ（**未着手**）

**`persona_context_build_ms` を測ってから。** 有意でなければ作らない。
動的状態（会話履歴・感情・関係値・World State・取得Memory）を
入れない。

---

## 3. 未解決の問題（実機で出ているもの）

| 問題 | 状態 |
|---|---|
| たまに同じ文を繰り返す | **原因未特定。** 分類は実経路へ接続済で、`turn_frame_enabled: true`（2026-08-04）なので**次に出たら層が記録される** |
| 応答開始が 4000〜5000ms | **未実測。** 測り方は直した（①で数字を取る） |
| ペルソナ変更後に旧ペルソナらしい反応 | 会話履歴の引き継ぎは修正済み。記憶側は⑤⑥で読み書き両方を分離し、旧記憶214件も移行して **`persona_scope_enabled: true` を本番有効にした**。**実機での成立確認（⓪スモークテスト）が未了** |

---

## 4. 守ること（チビからの継続指示）

- **設定ファイルのデフォルト値を勝手に本番有効へ変更しない。**
  Phase 7 / 7B の機能フラグは**全て false** のまま。
  例外: `memory.episodic_enabled` / `retrieval_enabled` /
  `turn_integrity.turn_frame_enabled` / `persona_scope_enabled` は
  **2026-08-04 にチビの明示指示で true にした**（旧記憶の移行完了が前提）。
  `reflection_enabled` / `duplicate_suppression_enabled` /
  `allow_legacy_unscoped_memory` は引き続き false。
- persona は変更されても問題ない。
- `AGENTS.md` の禁止事項（Git 操作・秘密情報の複製・
  Local/Discord 片側だけの変更・変更履歴の未更新で完了報告）。
- 憲法と衝突する提案は、黙って再設計せず
  `CONSTITUTION_AMENDMENT_PROPOSAL` として提示する。
- コード全文や巨大 diff を報告に貼らない。

---

## 5. Phase 8 について

**⑤⑥はコード上は閉じたが、運用上はまだ。** ⓪のスモークテストで
`persona_scope_enabled: true` の分離が実機で成立することを確かめ、
①の実測値が出るまで Phase 8 へ進まない。

Phase 8 の内容はまだ渡されていない。渡された時点で、
このファイルの §2 がどこまで消化できているかを先に確認すること。
**書き込み側のペルソナ分離が空のまま自律機能へ進むと、
新しい記憶が全部「所有者不明」で溜まる。**

---

## 6. サンドボックスでのテスト手順

Windows 側では動かないので、Linux サンドボックスへ複製して回す。

```
rm -rf /tmp/p7 && mkdir -p /tmp/p7
cp -r neuro_voice tests config data game_profiles docs /tmp/p7/
```

`/tmp/p7/conftest.py` に Python 3.10 用の `StrEnum` shim を置く:

```python
import sys, enum
if not hasattr(enum, "StrEnum"):
    class StrEnum(str, enum.Enum):
        def __str__(self): return self.value
    enum.StrEnum = StrEnum
sys.path[:0] = ["/tmp/stub", "/tmp/stub2"]
```

スタブ: `/tmp/stub`（sounddevice）、`/tmp/stub2`（openai）。

```
cd /tmp/p7 && PYTHONPATH=/tmp/p7 python3 -m pytest tests/ -q \
  -p no:cacheprovider \
  --ignore=tests/test_style_bert_vits2.py \
  --ignore=tests/test_ui_mind_status.py \
  --ignore=tests/test_discord_screen_share.py \
  --ignore=tests/test_local_audio_liveness.py \
  --ignore=tests/test_remote_launcher.py
```

**注意**: `bash` の実行枠が45秒なので、全体回帰は `timeout 42` を
付ける。約33〜41秒で終わる。

診断の一括実行:

```python
from neuro_voice.diagnostics import wiring
r = wiring.run_all(cfg)     # cfg は config.yaml をフラット化したもの
print(r.counts(), len(r.snapshot()["attention"]))
```

**要注意 0 件**を保つこと。warm sweep は 250ms 以内。

---

## 7. 作業のやり方（これまで効いたこと）

- **故障注入を必ずやる。** 「実装がある」と「効いている」は違う。
  ガードを1つ消して、テストとプローブが**赤くなること**を確認する。
  この習慣で、これまでに**自分のバグを9件**見つけている
  （指紋に participant_ids、redact が禁止語方式、
  強調ガードが一度も通っていない、`suppress` が `TypeError` を
  飲んで検索が0件、**プローブの冪等性チェックが「2回目の戻り値」と
  「今入っている物」を比べていて常に一致していた**、など）。
- **プローブはソースを読むだけにしない。** 実際に往復させる。
  読むだけの点検は故障注入をすり抜ける（`migration.single_start` で
  一度踏んだ）。
- **handoff の「残っている穴」を書きながら見直す。**
  「UI が呼んでいない」と書いた時点で、それは残件ではなく
  **未完成**だと気づいた例が2回ある。
- 実機でしか判断できないもの（自然さ・音質・体感速度）は、
  **こちらで「問題ない」と書かない。** チビに聞く。

---

## 8. 主要ファイルの地図（Phase 6〜7D で足したもの）

```
neuro_voice/cognition/
  world.py            Grounded World State
  goals.py            GoalRecord / 権限判定（7分類）
  identity.py         Identity 三層 / IdentityLink
  presence.py         誰が居るか
  dedup.py            記憶の指紋と重複排除
  tools.py            Tool Capability Registry（副作用6分類）
  planning.py         ActionIntent / BoundedPlan / Admission Gate
  tool_exec.py        Confirmation / Tool Gate / 冪等 / 実行
  tool_report.py      ToolOutcomeEvent / 信頼境界 / 報告候補
  tool_runtime.py     一式を束ねて実経路へ
  scratch_tool.py     試験用の実書き込み（パス安全性）
  turn_integrity.py   TurnFrame / 重複4分類 / CommitLedger
  turn_tracker.py     上記を実経路へ挿す層（Local/Discord 共通）
  turn_latency.py     集計側（p50/p90/p95・機能別比較）
  persona_scope.py    6スコープ / PersonaSwitch / epoch
  rollout.py          Speech Gate（発話元ごとの条件）

neuro_voice/utils/latency.py   **実経路の計測の背骨**（7D で拡張）
neuro_voice/mind/migration.py  関係値の人物単位移行
neuro_voice/mind/relationship.py  関係値の持ち主（7D ⑥）
neuro_voice/mind/legacy_scope_migration.py  旧記憶の帰属移行
neuro_voice/cognition/trace.py  分離の観測点（persona_leak）
neuro_voice/diagnostics/wiring.py  110プローブ
neuro_voice/mind/store.py      持ち主とスコープを決める1箇所（7D ⑤）

docs/audits/2026-08-03_turn_path.md   実経路の地図
```
