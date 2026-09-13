# 2026-09-09 03:40 Sol — Stage M final-review close

## 担当・目的

- 担当: Sol（Stage M review-fix v2の実装・検証・文書同期）。
- 目的: 独立再レビューで残ったsemantic false-negative、transcript未配線、sidecar不正VIEW、query-only startup、production-limitでないbenchmark、sidecar open時差替えを実動RED→GREENで閉じる。
- 範囲: Stage Mのみ。P3以降、本番DB/config/persona/過去Trace、実機、外部I/O、永続`memory.write_enabled`は変更していない。

## 根本原因と修正

1. 旧semantic sidecarは一部次元を固定量子化したbandだった。cosine 0.99985級でも量子化境界を跨ぐと全bucket不一致になり、同bucketが300件あるとper-band LIMITより後ろの正解を失った。
   - schema generation 5で、全384次元をSHA-256由来のdeterministic Rademacher hyperplane 64本へ射影するANNへ置換した。
   - 4表×16-bit bucketのexact＋Hamming距離1だけをprobeする。候補は実働`top_k=5`でMemory最大80、transcript最大40に制限し、最終contextは各5件。
   - exact embedding hashは高速pathとして維持。候補後はcanonical persona/scope/status/vectorを再取得しcosine順位付けする。`all_embeddings`/全transcript embedding scanは実経路から使わない。
2. transcript semanticはsidecar APIが実働`Mind._transcript_matrix/_search_transcripts`へ未配線だった。
   - transcript用ANN派生tableを同sidecarへ追加し、Memoryと同じbounded candidate→canonical再検査へ接続した。
3. valid SQLiteでも`memory_search_docs`がVIEW等の不正objectだと、schema部分dropだけでは復旧不能だった。
   - 派生connectionを閉じ、sidecarファイル全体だけをunlink→一回再作成する。canonical sourceは削除しない。再試行は一回だけ。
4. query-only sourceの`__init__`が無条件migration/metadata INSERTでraw `OperationalError`になり、connection cleanupも明示されなかった。
   - 完全migration済みschemaならprocess-local read-onlyへ退避しretrievalを継続、healthはcontent-free `read_only`。
   - migration必須schemaで書けない場合はconnectionを閉じ、`MemoryStoreMigrationRequired`を返す。通常writable migrationは維持。
5. benchmarkが候補8件という旧直接APIを測り、production Mind経路・transcript・最終contextを分離していなかった。
   - production `Mind`のMemory候補80、transcript候補40、最終各5を実経路で計測し、lexical/semantic/transcript、初回build、Recall/restart再構築を別記録した。
6. sidecar pathはopen前だけの検査でcheck→connect間の差替え窓が残った。
   - open後とschema write直前にもresolve/file identity/nlinkを再検査し、不一致なら閉じて派生fileだけを切り離す。
7. 通常Recallはboundedでも、旧`_warmup_embedder`が起動時に全Memory/transcript embeddingをRAM展開していた。
   - warmupをembedderモデルの一件入力だけに限定し、永続vectorはquery時のbounded sidecar候補だけを使う。

## 変更ファイル

- `neuro_voice/mind/store.py`: deterministic dense ANN、Memory/transcript候補、sidecar generation 5、VIEW/破損file一回置換、open後/DDL前identity、query-only startup、typed migration error。
- `neuro_voice/mind/mind.py`: Memory/transcript semantic candidateのproduction経路配線。
- `tests/test_memory_retention_contract.py`: 高cosine境界、300衝突後方、transcript実経路、VIEW repair、query-only current/old、connection close、link swap等のRED/GREEN。
- `tools/benchmark_memory_retention.py`: synthetic Memory/transcriptをproduction Mind limitで測るbenchmark。
- `docs/SHARED_CHANGELOG.md`、`CURRENT_ARCHITECTURE.md`、`CODEMAP.md`、`DECISION_LOG.md`、`KNOWN_RISKS_AND_DEBT.md`、`adr/ADR-0005-conversation-quality-recovery.md`、`superpowers/plans/2026-09-07-conversation-quality-sol.md`、`handoffs/_NEXT_SESSION.md`: current fact/decision/risk/入口を同期。

## RED→GREEN

- 初回review RED: write-disabled touch、poisoned archived status、corrupt sidecar restart、語彙違いsemanticの4件。前review-fixでGREEN。
- final review RED: 高cosine quantization boundary、300 legacy bucket collision後方、transcript semantic未配線、VIEW wrong-schema repair、current-schema query-only startup、old-schema query-only safe failure/closeの6件。
- link swapのunitも追加し、Windowsでsymlink作成権限がない場合だけ明示skip。path validation unit自体は継続GREEN。
- 最終Stage M＋関連: `115 passed, 1 skipped in 13.09s`。
- `py_compile`: `store.py`、`mind.py`、benchmark、contract testが成功。
- 最終全pytest: `3055 passed, 2 skipped in 112.58s`。skipは2件ともWindows symlink privilege unavailable。

## 隔離benchmark

実行: `.venv-test\\Scripts\\python.exe tools\\benchmark_memory_retention.py --rounds 9`。全てtmp_path相当の一時DB、384次元synthetic vector、各規模でMemoryとtranscriptを同数生成。source件数は前後不変。

| 各source件数 | lexical p50/p95 ms | Mind semantic p50/p95 ms | transcript候補 p50/p95 ms | startup ms | build ms | Python alloc peak bytes | sidecar bytes |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.278 / 18.494 | 9.675 / 10.688 | 4.710 / 4.880 | 5.675 | 655.654 | 10,471,913 | 1,499,136 |
| 10,000 | 1.461 / 4.739 | 10.115 / 10.727 | 4.920 / 5.168 | 5.599 | 6,409.468 | 42,325,315 | 14,643,200 |
| 100,000 | 1.745 / 6.766 | 10.738 / 11.576 | 5.160 / 5.383 | 5.478 | 65,934.354 | 365,064,033 | 150,904,832 |

- Memory候補 limit/count=80/1、transcript候補=40/1、最終context各limit/count=5/1。
- 連続Recall中の不要rebuild=0、touch後restartの不要rebuild=0。
- buildは派生sidecarの初回/対象field更新後コスト。100kはMemory 10万＋transcript 10万、計20万canonical source行をindex化している。
- `startup ms`は`MemoryStore` reopenだけで、embedderモデルloadを含むアプリ全体のstartupではない。
- 9 roundsのp95は探索値で本番SLOではない。`tracemalloc`値はPython allocationのみで、OS RSS、SQLite/filesystem page cacheを含まない。

## 設定・DB migration・保護対象

- 設定変更なし。永続`memory.write_enabled`変更なし。
- canonical schemaの新規migrationなし。sidecar generationだけ5へ更新し、旧派生物は安全に再構築される。
- 本番DB/data、persona、config、過去Trace、raw audio/image、外部serviceを開いていない。
- 故障注入は一時DBだけ。query-only/SQLITE_FULL後もcanonical rowは保持され、派生修復はcanonicalへ触れない。

## 未解決・確認事項

1. 確認付き自然言語削除UXは未実装。storage primitiveとDO_NOT_STOREは維持するが、破壊操作のproduction intent callerはユーザー承認を得て別設計する。
2. transcript全文の最終保持範囲、backup配置、disk容量/警告閾値は所有者確認待ち。
3. 本番既存DBで過去に自動削除が起きたかは未調査。本番接続はbackup、dry-run、before/after件数・指紋照合を揃えた別承認が必要。
4. 100k＋100kの派生buildは約65.9秒、Python allocation peak約365MB。実データ分布、OS RSS/page cache、並行前景負荷は未測定。
5. open前後/DDL前のidentity検査で競合窓を狭めたが、悪意ある同directory同時writerによるpath-name SQLite openの完全防御はできない。sidecar directoryを同一の信頼ownerだけが書ける権限で運用する。

## 実機最小手順（別承認後）

1. 本番DBを停止状態でbackupし、件数・ID/本文/embedding指紋をdry-run取得する。
2. 既存sidecarだけを再構築対象にして一回起動し、storage health、build時間、OS RSS、disk増加を確認する。
3. 古い語彙違いMemory 1件とtranscript 1件を明示Recallし、persona/status漏れ0、候補上限80/40、最終5を確認する。
4. 再起動と同じRecallを行い、不要rebuild 0、source件数・指紋不変を再照合する。

## Rollback

- `store.py`/`mind.py`/contract test/benchmarkと本handoff記載のdocsだけをこのStage M final-review前へ戻す。
- canonical sourceへは触れていないためdata rollbackは不要。generation 5 sidecarは派生物なので、停止中にそのファイルだけを除去すれば旧sourceを壊さず再構築可能。
- 旧sampled/quantized semanticへ戻すと今回の高cosine境界・衝突REDが再発するため、運用rollbackとしてはsemanticを無理に有効化せず既存lexical/recent fallbackへ戻す方が安全。

## 次のagentへの注意

- P3以降へ進む前に本handoffとD-045/R-049を読む。
- benchmarkの候補数と最終context数、初回buildとRecall latencyを混同しない。
- sidecarは派生物、canonical DBは正本。wrong-schema repairでcanonicalをunlink/dropしてはならない。
- 本番data操作、永続write設定、自然言語削除callerは本作業の承認範囲外。

関連: PROJECT_CONSTITUTION第11・12・17・21・22条、D-045、ADR-0005、R-049。
