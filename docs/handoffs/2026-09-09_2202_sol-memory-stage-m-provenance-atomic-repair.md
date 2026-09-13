# 2026-09-09 22:02 Sol — Stage M v5 provenance / atomic repair close

## 担当・範囲

- v5再レビューのImportant 2 / Minor 1をStage M内でTDD修正した。
- 本番DB/config/persona/過去Trace、実機、外部I/O、永続`memory.write_enabled`、P3以降は非接触。
- v1〜v4の非削除、write-disabled、source mutation失敗境界、freshness、ANN Recall、ACL/status/vector、sidecar schema/path/link安全性を再回帰した。

## REDと根本原因

1. v4はcanonical vectorのdim/length/finite/nonzeroとcandidate IDを検査したが、sidecarで選択したbucketがcanonical vectorから本当に導出されるかは検査していなかった。cosine -1のwell-formed vector 80/40件をquery bucketへ偽装すると後方cosine .90 targetが消え、wrong-vector forged exact hitも即returnした。Memory/transcript各2件、計4 RED。
2. v4 logical repairは`drop_search_index()`のcloseとunlinkの間でStore lockを解放した。barrierを入れると別public searchがその中間sidecarをopenできることをREDで再現した。
3. Windowsでsidecar unlinkが`PermissionError`になるとlogical repairとmalformed-schema repairからraw OS errorが漏れた。semantic/lexical各1 RED。
4. benchmarkはfast/fullの数値を分けていたが、target cosineと実際のprobe tier/path markerがなく、同じfast条件を別名で2回測る退行をreport contractが検出できなかった。

## 実装

- Memory/transcriptのANN nominationはsource IDだけでなく、選択に使ったband/bucket provenanceを保持する。exactはband 0のhash bucketを保持する。
- canonical hydrate後、候補vectorを一括でnormalise・dense projectionし、exact hashと全選択bucketをbatch再導出する。sidecar rowと一件でも不一致なら候補から除外し、派生sidecar全体だけを一度repairして同queryを最大一回再試行する。
- canonical persona/scope/requested status、ID、dim、float32 length、finite、nonzero検査は維持。canonical不正vectorはrebuild時に除外する。
- Store lockを`RLock`にし、logical/schema repairは単一ownerがclose→safe unlink→recreate/rebuildの全区間を保持する。public drop/rebuildを同じownerから再入して既存計測hookを維持し、並行public searchへ中間sidecarを公開しない。
- `OSError`/`PermissionError`はderived-index修復失敗へ正規化する。semanticは空候補、lexicalはbounded recent fallbackへ退避し、canonical sourceは変更しない。
- benchmark reportへ`fast_target_cosine=.99985`、`full_target_cosine=.90`、`Mind._recall_semantic`、`4-table-hamming-1`/`14-table-hamming-2`を追加した。

## RED → GREEN

- well-formed wrong-vector approximate cap: Memory80 / transcript40の2 RED → GREEN。
- well-formed wrong-vector forged exact: Memory / transcriptの2 RED → GREEN。
- barrier repair race: provenance修正後も競合REDを確認 → atomic repairでGREEN。
- logical repair `PermissionError`: RED → semantic空候補・source不変GREEN。
- malformed schema `PermissionError`: RED → lexical fallback・source不変GREEN。
- benchmark target/path/probe marker: KeyError RED → GREEN。

## ANN品質gate

既存fixed-seed 384次元1000組をcontractで再回帰した。

| cosine | hit / 1000 | gate |
|---:|---:|---:|
| .72 | 956 | >=950 |
| .80 | 992 | >=980 |
| .90 | 1000 | >=995 |
| .95 | 1000 | 1000 |
| .99 | 1000 | 1000 |

Memory候補80、transcript40、context各5、fast各表256行、full各表1280行、毎turn全embedding scan禁止は不変。

## 100k＋100k benchmark

実行: `.venv-test\Scripts\python.exe tools\benchmark_memory_retention.py --rounds 9`。一時DBのみ。

| 各source件数 | lexical p50/p95 ms | fast .99985 Mind | fast transcript | full .90 Mind | full transcript | build ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.667 / 20.916 | 10.210 / 10.512 | 4.729 / 5.301 | 112.947 / 125.489 | 51.977 / 65.177 | 1,920.444 |
| 10,000 | 2.113 / 6.114 | 11.173 / 11.686 | 5.525 / 6.593 | 185.011 / 235.821 | 65.415 / 101.426 | 21,155.518 |
| 100,000 | 2.419 / 6.517 | 10.654 / 12.419 | 5.292 / 6.610 | 375.196 / 392.688 | 154.252 / 174.066 | 214,969.404 |

- 100k fast候補Memory1/transcript1、full候補80/40、最終context各1/上限5。
- source Memory/transcriptは各規模before/after不変。Recall中/touch後restart rebuild 0/0。
- 100k sidecar 320,561,152 bytes、Python allocation peak 370,916,986 bytes、store reopen 7.380ms。
- full tierは14表×137 multiprobe、高衝突fixture、canonical provenance batch再導出を含む品質/integrityコスト。v4の225.459msから392.688msへ増えた値を隠さず記録し、一般semantic latencyまたは本番SLOとはしない。
- 9 roundsは探索値。RAMは`tracemalloc`のPython allocationのみでOS RSS/SQLite/filesystem page cacheを含まない。reopenはembedder model loadを含まない。

## Verification

- v5 focused: `8 passed`相当の各RED→GREENを個別確認。統合filterは`7 passed`、malformed PermissionErrorは単独`1 passed`。
- Stage M contract: `56 passed, 1 skipped in 20.05s`。
- Stage M＋Memory関連: `183 passed, 1 skipped in 45.66s`。
- py_compile: `store.py`、benchmark、contract test成功。
- 全pytest: `3070 passed, 2 skipped in 134.89s`。
- skipはWindows symlink privilege unavailable 2件。mock/path validationは実行済み。

## 変更ファイル

- `neuro_voice/mind/store.py`
- `tests/test_memory_retention_contract.py`
- `tools/benchmark_memory_retention.py`
- `docs/SHARED_CHANGELOG.md`
- `docs/CURRENT_ARCHITECTURE.md`
- `docs/CODEMAP.md`
- `docs/DECISION_LOG.md`
- `docs/KNOWN_RISKS_AND_DEBT.md`
- `docs/adr/ADR-0005-conversation-quality-recovery.md`
- `docs/superpowers/plans/2026-09-07-conversation-quality-sol.md`
- `docs/handoffs/_NEXT_SESSION.md`
- 本handoff。

## 保護対象監査

- `config/config.yaml`、data最新、`logs/cognitive_trace.jsonl`のmtimeは作業前値から不変。
- test/benchmarkはpytest/tmpの一時DBのみ。canonical本番DB、persona、config、Trace、raw audio/imageへ非接触。
- git init/status/commit/reset/pushは実行していない。

## 保証境界

- sidecarはauthorityではない。canonical ACL/status/vectorと、選択されたsidecar provenanceを再検査するため、poisoned rowから別persona・archived・wrong vectorを正当候補として返さない。
- 同じOS directoryへ書ける悪意あるownerがsidecar rowとmetadataを任意に一貫改変・削除した場合、検出不能な候補omissionによる可用性低下までは保証しない。完全防御にはsidecarへの署名/別権限owner等が必要でStage M範囲外。
- 現防御はdirectoryを信頼ownerだけに限定し、欠落疑い時に派生sidecarを明示rebuildすること。

## 未解決・後日確認事項

1. full tierの実モデル/実データ分布でのRecall/latency。今回の392.688ms p95は高衝突synthetic探索値で、速度優先要件との判断が必要。
2. 各100k＋100k build中のsource lock約215秒と約320.6MB sidecar。write-on運用ならsnapshot/online rebuildを別設計する。
3. 確認付き自然言語削除UXは未実装。storage primitiveとDO_NOT_STOREのみ維持。
4. transcript全文保持範囲、backup配置、disk監視、本番既存DB移行は別承認事項。
5. 同権限悪意writerの検出不能omissionとpath-name SQLite open TOCTOU完全防御。

## 実機最小手順（別承認後）

1. 停止中の本番DBをbackupし、before件数・ID/本文/embedding指紋を取得。
2. 隔離コピーでgeneration 8 sidecarをbuildし、時間、OS RSS、disk、source lock影響を測る。
3. fast/full相当の既知Memory/transcriptをRecallし、候補80/40、context5、persona/status漏れ0、provenance repair回数を確認。
4. sidecar明示rebuildと再起動後のrebuild 0、source件数・指紋不変を確認して本番昇格を判断。

## Rollback

- v5のStore/test/benchmark/docsだけをv4へ戻せる。canonical source変更なし。
- generation 8 sidecarは停止中に派生ファイルだけ削除し、canonical DBから再構築可能。
- provenance/atomic repairを戻すとwell-formed poisonと中間open競合が再発するため、異常時はsemanticを無理に使わずlexical/recent fallbackへ退避する。

関連: PROJECT_CONSTITUTION第11・12・16・17・22条、D-045、ADR-0005、R-049。
