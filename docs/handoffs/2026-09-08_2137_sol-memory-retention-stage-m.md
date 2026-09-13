# Sol handoff — Stage M 非削除Memory保持とbounded検索

- Date: 2026-09-08 21:45 JST
- 担当: Sol（前Sol partial diffの継承・再検査・完成）
- Status: `CODE_VERIFIED / PRODUCTION_DATA_UNTOUCHED`
- Scope: 計画の独立単位Mのみ。P3以降、実機、本番data移行は未着手。

## 結果

canonicalな`memories`/`transcripts`を件数・経過日数・importanceだけでは削除しない保持契約へ変更した。旧startup/maintenanceの`prune_transcripts()`/`prune()`入口は単一`RetentionPolicy`の非削除契約を通る。明示的なユーザー削除、privacy訂正、DO_NOT_STOREは別境界として維持する。

検索量は保持量から分離した。`*.search-index`は削除・再構築可能なFTS5 sidecarで、必要性gate後にpersona/scope ACL付きのbounded top-kを返す。canonical source取得時にも同じACLを再検査する。明示Recallはarchivedを含む古いIDへ到達できる。index破損はrestart後に自動再構築し、sourceは変更しない。

SQLite source write失敗はrollbackし、`MemoryWriteUnavailable`を返してそのprocessのstoreをread-onlyへ倒す。空き容量確保のため既存sourceを自動削除しない。backup作成失敗は成功signature/zipを残さない。

## partial diffの再監査と根本原因

引継ぎ時点のcontract test 74件はGREENだったが、次の穴を追加REDで検出した。

1. sidecarのpersona列だけでACLを確定し、候補IDからcanonical行を読むSQLでpersona/scopeを再検査していなかった。stale/破損/改変sidecarが別persona IDを返すとsource本文が返り得た。
2. malformed sidecarを開くとbounded recent fallbackへ退避するだけだった。対象が古い場合、canonical sourceが無傷でも明示Recallから見えなくなった。

修正は、canonical row queryへのACL再適用と、sidecar open失敗時のconnection close→derived file削除→sourceからの一回再構築。indexは権限正本ではなく性能用派生物、という境界をコードで固定した。

テスト側では、FTSのOR-bigram検索が複数候補を返し得るのに単一候補だけを期待したassertを一件修正した。これは実装欠陥ではなくテスト期待の誤り。benchmarkは最初、fixture行listを作った後に`tracemalloc`を始めてRAMを過少表示していたためRED化し、dataset構築・index・restart・query全体へ測定範囲を拡張した。直接script起動時のproject import欠落も修正した。

## 変更ファイル

実装・テスト:

- `neuro_voice/mind/store.py`
- `neuro_voice/mind/episodes.py`
- `neuro_voice/mind/mind.py`
- `tools/benchmark_memory_retention.py`
- `tests/test_memory_retention_contract.py`
- `tests/test_episodic_memory.py`
- `tests/test_transcript_memory.py`

文書:

- `docs/superpowers/plans/2026-09-07-conversation-quality-sol.md`
- `docs/adr/ADR-0005-conversation-quality-recovery.md`
- `docs/CURRENT_ARCHITECTURE.md`
- `docs/CODEMAP.md`
- `docs/DECISION_LOG.md`
- `docs/KNOWN_RISKS_AND_DEBT.md`
- `docs/SHARED_CHANGELOG.md`
- `docs/handoffs/_NEXT_SESSION.md`
- 本handoff

## RED / GREEN / verification

- 引継ぎbaseline: Memory関連3ファイル `74 passed`。
- RED 1: poisoned derived indexからpersona `c`のprivate sourceがpersona `b`検索へ返った。
- RED 2: corrupt sidecar restart後、古い明示Recallがbounded recent fallbackから脱落した。
- RED 3（計測契約）: benchmarkにRAM測定範囲がなく、100k fixture構築を除外していた。
- GREEN: Stage M contract `19 passed`。
- 関連Memory/backup/DO_NOT_STORE: `124 passed`。
- persona write scope / legacy migration / relationship persona key: `46 passed`。
- 失敗した検証コマンド: 最初の関連targeted一覧に存在しない`tests/test_persona_scope.py`を指定して`no tests ran`。実在する`test_persona_write_scope.py`、`test_legacy_scope_migration.py`、`test_relationship_persona_key.py`へ訂正し46件GREENを確認した。
- py_compile: `store.py`、`episodes.py`、`mind.py`、benchmark、Stage M testすべてexit 0。
- 最終全pytest: `3033 passed, 1 skipped in 110.16s`。skipは`tests/test_tool_dialogue.py:663`のWindows symlink権限のみ。

故障注入はすべてtmp_path。実SQLite `SQLITE_FULL`、query-only write failure、poisoned sidecar、corrupt sidecar、backup copy failureを復元後GREENにした。repo内へfault用DB/sidecar/backupを残していない。

## 1k / 10k / 100k 隔離benchmark

`.venv-test\Scripts\python.exe tools\benchmark_memory_retention.py --rounds 9`。production DB pathを受け取らず、temporary directoryだけを使う。

| source件数 | query p50 | query p95 | startup | index build | peak RAM | index bytes | source before→after |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.267ms | 16.420ms | 4.811ms | 81.301ms | 834,747 | 311,296 | 1,000→1,000 |
| 10,000 | 1.279ms | 3.151ms | 4.772ms | 415.873ms | 2,784,300 | 2,822,144 | 10,000→10,000 |
| 100,000 | 1.624ms | 3.838ms | 5.158ms | 4,711.074ms | 23,813,074 | 30,695,424 | 100,000→100,000 |

各queryの候補は1件（contract上限8）。peak RAMはdataset build、index build、restart、queriesを含む。単発の端末探索値であり本番SLOではない。1k p95の揺れもあるため、規模間の厳密比較には繰返し測定が必要。

## 非変更・安全確認

- `config/config.yaml`、persona JSON、`data/`の本番DB/backup、`logs/cognitive_trace.jsonl`、`.env`を開いて変更していない。
- 永続`memory.write_enabled`を変更していない。Local/Discord起動、LLM/TTS、外部network、クラウドAPIは未実行。
- schema migration、production backup、dry-run、実データfingerprintは実行していない。
- P3以降、検索実機受入、Fish Audio、人格調整へ進んでいない。

## rollback

Git rootは無効なのでcommit/resetは使わない。Stage Mを戻す場合は上記7実装/testファイルの2026-09-08 hunkと同期文書を手作業で戻す。ただし旧`prune`の自動DELETE、全件materialization、persona ACL bypass、破損indexで古いRecall喪失が再発する。DB/config/persona/Traceをrollback対象に含めない。

## 未解決・後日確認事項

- Memory本体とは別に、永続transcript全文も無期限retainするか。
- backup配置、容量上限の通知先、disk監視閾値。`storage_health()`はcontent-freeだがoperator UIへの常設表示は未実装。
- 既存本番DBへpolicyを適用する際のbackup/dry-run/件数・指紋照合と実行承認。過去に旧pruneで削除された記録の有無は不明で、復元を推測しない。
- 100k超および長期連続運用のindex build時間・disk成長は未測定。

## 次の最小手順

ユーザー確認前に本番DBへ触れない。transcript/backup/disk監視方針が決まった後、別承認の作業としてproduction backup→read-only dry-run→before/after件数・source fingerprint照合→一回再起動を行う。その後にP0〜P2 handoffのLocal検索2発話をユーザー操作で受け入れる。

## 関連

- `docs/superpowers/plans/2026-09-07-conversation-quality-sol.md` §M
- `docs/adr/ADR-0005-conversation-quality-recovery.md`
- Decision D-045、Risk R-049
