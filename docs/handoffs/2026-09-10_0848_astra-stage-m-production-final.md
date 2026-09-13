# Stage M 本番昇格・容量警告 最終検証 handoff

- Date: 2026-09-10 08:48 JST
- Agent: Astra（運用・独立検証）、Sol（コード変更）
- Status: `CODE_VERIFIED / USER-APPROVED PRODUCTION PROMOTION COMPLETE / LOCAL HARDWARE PENDING`

## 結果

ユーザーの明示承認を受け、停止中の本番Memory DB 7件をSQLite online backupし、隔離copyでmigrationとgeneration 8 sidecarを検証してから本番へ昇格した。canonical Memory/transcriptの件数およびID・本文・summary・embedding指紋は全工程で不変。アプリ起動・実機発話・外部検索はまだ行っていない。

全文Memory/transcriptは無期限`retain`、backupはプロジェクト内、容量警告は空き20%未満という承認済み方針である。警告は観測専用で、低容量またはprobe不能を理由に自動削除・archive・write停止しない。

## Backupと本番昇格

- AItuber停止確認: `python` / `pythonw`なし、全`mind_*.db`が排他読取可能。
- Backup: `data/backups/memory_retention_stage_m/20260910_074937_preindex`
- 対象: `mind_a/b/c/luna/momo/neru/neuro.db`の7件。
- 件数: Memory 232件、transcript 1,142件。
- 全7 DB: `quick_check` / `integrity_check` OK。
- 証跡: `manifest.json`、`isolated_validation.json`、`production_promotion.json`。本文そのものは証跡へ複製せず、件数と指紋だけを保存。
- 最初のbackup試行はWindowsの一時SQLite handleが閉じる前のrenameで停止した。作成途中の専用directoryだけを解決済み絶対path確認後に削除し、明示close版で再実行。本番sourceは変更されていない。
- 隔離copy: raw/unit/cosine .90の既知Memory/transcript vectorを各2回Recall。候補上限80/40、最終context 5、persona/status漏れ0、Recall中/restart後rebuild 0/0、backup SHA不変。
- 本番migration: `memory_search_state`とrevision triggers、一部旧DBの`reflections.persona_id/scope`を追加。generation 8 sidecarを7件作成。
- 本番結果: schema current、canonical指紋不変、sidecar合計2,797,568 bytes、restart rebuild 0、既知vector Recall成功。

## 容量警告

Solが`MemoryStore.storage_health().capacity`を追加し、共有`Mind.status()`からLocal/Discordへ同じ意味で公開した。「こころ」画面は状態、空き率、閾値、測定先を表示する。

- canonical/data/backup/sidecarを所在volume単位で測定。
- 既定20%未満で`warning`。`mind.storage.warning_free_percent`で変更可能だが永続configには書いていない。
- probe失敗は`unknown`。会話、保持、write可否を変更しない。
- 実測C drive空きは525.73 GB / 28.24%で、現在は閾値超過。
- 独立レビューで、`null`をUIが0.0%表示する問題と、19.999%を丸めて20.0%/OKにする問題を発見。Solが実JS描画とraw比率判定へ修正した。
- 再レビュー: Critical 0 / Important 0 / Minor 0。

## 最終検証

- `py_compile`: 成功。
- focused: `67 passed, 1 expected Windows symlink skip in 19.06s`。
- Memory/transcript/persona/backup/UI関連: `255 passed, 1 expected skip in 45.55s`。
- 全pytest: `3078 passed, 2 expected Windows symlink skips in 116.74s`。
- failed: 0。

## 非変更対象

- 永続`memory.write_enabled`、persona設定、既存Trace、raw audio/imageは変更していない。
- アプリとDiscordは起動していない。外部検索・有料APIなし。
- Git操作なし。

## 残存リスク

- 警告はoperatorが「こころ」またはstatusを開いた時の表示で、OS通知や自動回収ではない。
- 各100k＋100kのgeneration 8 buildはsyntheticで約209秒source lockを保持し、sidecar約320.6MB。本番実分布のfull-tier性能は未測定。
- 確認付き自然言語削除UXは未実装。
- 同directoryへ書ける同権限の悪意ownerによる検出不能なsidecar omissionは可用性保証外。

## 次の最小実機手順

ユーザー操作でLocalを起動し、次を一回ずつ発話する。自動で30 turnへ拡大しない。

1. `饅頭こわいを検索して、短く教えて`
   - 検索一回、作品に関係する根拠付き短答、UI source一件、logical playback session一回、replay 0、finalizedを確認。
2. `さっきの話の結末も分かる？`
   - 同epoch Evidenceで答えられる範囲だけを答え、不足時は不足を明示し、題名から筋書きを創作しない。

2発話が通るまでP3以降やDiscord複数人受入へ進まない。
