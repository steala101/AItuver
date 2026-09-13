# Sol handoff — Stage M 独立レビュー修正

- Date: 2026-09-09 02:41 JST
- Status: `CODE_VERIFIED / PRODUCTION_DATA_UNTOUCHED`
- Scope: 計画Mのみ。P3以降、実機、本番data移行は未着手。

## 結果と根本原因

独立レビュー7項目を実経路で再現し、最初の追加contractは`13 failed / 20 passed / 1 symlink skip`だった。主因は、保持policyの初回実装がinsert/deleteの失敗境界とlexical sidecarには届いた一方、既存行mutation、write-disabled retrieval、status正本再検査、semantic候補経路、mtime非依存freshness、valid-but-wrong schema、linked pathを一つのStage M契約として閉じていなかったこと。

`MemoryStore._source_write`の可否判定をlock内へ移し、`touch/reinforce_episode/mark_episode/upgrade_episode`を同じrollback→process read-only境界へ収束した。`EpisodicMemoryService.retrieve`とMind lexical/semanticはいずれも`memory.write_enabled=false`ならtouchしない。

freshnessはmtime/source signatureでなくcanonical `memory_search_state.revision`へ変更した。triggerは本文、ACL、status、created time、embedding等のindex対象列だけでrevisionを進め、access bookkeepingやinformation type/confidenceは進めない。これにより連続Recallとtouch後再起動はrebuild 0、insert/status/deleteは次の検索で更新される。

semantic Recallは`Mind._recall_semantic`から`search_episode_embeddings`へ実配線した。sidecarにexact hash＋4 sampled/0.1量子化bandを持ち、exactは早期終了、近似は各bandを32〜256件で事前LIMITする。canonical embedding全走査は行わず、候補後にcanonical persona/scope/statusを再検査し、最後のcosine順位はMindが所有する。「自動車が好き」対「クルマの好み」を別成分+0.02の近似vectorで古いIDへ到達し、`all_embeddings`を例外化しても成功した。

sidecarはmalformed DBおよびvalid SQLiteの不正FTS schemaを検出し、派生schemaだけを一回再構築する。canonicalと異なるdirectoryは拒否し、symlink/hardlinkはopen前に切り離す。Windowsでsymlink作成だけ権限skipだが、hardlinkとpath validationは実行済み。

## RED→GREENと検証

- RED: write-disabled Episodic touch、4既存行mutationの生SQLite例外、lock待ちwriter race、poisoned status、touch/restart再構築、非index upgrade再構築、wrong schema未修復、語彙違いsemantic欠落、hardlink汚染、path escape API欠落の計13件。
- GREEN: Stage M contract `33 passed / 1 expected Windows symlink skip`。
- 関連: retention + episodic + transcript + retrieval wiring `107 passed / 1同skip`。
- py_compile: `store.py`, `episodes.py`, `mind.py`, benchmark, contract test、exit 0。
- 最終全pytest: `3047 passed / 2 skipped in 105.74s`。skipはStage M sidecar symlinkと既存Tool path symlinkの`WinError 1314`だけ。
- fault injectionはすべてtmp_pathで復元後GREEN。外部I/Oなし。

## synthetic実vector benchmark

`.venv-test\Scripts\python.exe tools\benchmark_memory_retention.py --rounds 9`。production pathを受け取らない。

| rows | lexical p50/p95 | semantic p50/p95 | startup | build | Python alloc peak | sidecar | rebuild recall/restart |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.112 / 16.372ms | 4.122 / 4.342ms | 5.452ms | 386.635ms | 1,774,985 | 937,984 | 0 / 0 |
| 10,000 | 1.185 / 3.882ms | 4.277 / 4.482ms | 5.430ms | 3,462.661ms | 4,419,557 | 9,023,488 | 0 / 0 |
| 100,000 | 1.497 / 4.494ms | 4.331 / 4.521ms | 5.634ms | 36,417.474ms | 26,701,179 | 92,069,888 | 0 / 0 |

全件でsource count不変、lexical候補1、semantic候補8。RAMは`tracemalloc`が見るPython allocationだけで、OS RSSとSQLite/filesystem page cacheを含まない。p95は9 query roundだけの端末探索値で、統計的tail保証や本番SLOではない。semantic全GROUP+sort案は100k p50 848.727msを検出したため採用せず、band事前LIMIT後に4.331msへ修正した。一回build 36.4秒と約92MB sidecarは残るtrade-off。

## 変更ファイル

- 実装: `neuro_voice/mind/store.py`, `neuro_voice/mind/episodes.py`, `neuro_voice/mind/mind.py`, `tools/benchmark_memory_retention.py`
- test: `tests/test_memory_retention_contract.py`
- docs: plan, ADR-0005, CURRENT, CODEMAP, DECISION_LOG, KNOWN_RISKS, SHARED_CHANGELOG, `_NEXT_SESSION`, 本handoff

## 非変更・未解決

- `config/config.yaml`、persona JSON、`data/`本番DB/backup、過去Trace、`.env`、永続`memory.write_enabled`は変更していない。Local/Discord/LLM/TTS/外部network/クラウドAPIは未実行。
- storage primitiveとDO_NOT_STOREは維持したが、確認付き自然言語削除のproduction callerは未実装。破壊操作なのでユーザー承認を得た別UXとして設計する。
- 全文transcriptの最終保持範囲、backup配置、disk監視閾値/通知先、本番DBへの適用、100k超のbuild/disk成長は未解決。

## 実機・本番の最小手順（別承認後）

1. production backupを取得し、read-only dry-runでmigration予定だけを表示する。
2. before/afterのsource件数と本文/embedding指紋を照合し、sidecarだけを一回buildする。build中は会話を開始しない。
3. 再起動後にLocalで既知の古い記憶、語彙違いの質問、write-disabled Recallを各1件確認し、rebuild countとcontent-free storage healthだけを見る。
4. その後にDiscord複数人のpersona分離を確認する。自然言語削除試験は確認UX承認まで行わない。
