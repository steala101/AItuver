# 2026-09-10 02:10 Sol — Stage M v6 canonical vector単一正規化

## 担当・範囲

- v6再レビューのImportant 1をStage M内でTDD修正した。
- 本番DB/config/persona/過去Trace、実機、外部I/O、永続`memory.write_enabled`、P3以降は非接触。
- v1〜v5の非削除、write-disabled、source write failure、freshness、ANN Recall、ACL/status/vector、wrong-vector provenance、atomic repair、sidecar schema/path/link安全性を再回帰した。

## REDと根本原因

seed55の384次元vector（先頭4要素だけ`standard_normal`、残り0）はfloat32正規化で`N(v) != N(N(v))`となる。v5はsidecar build時にcanonical rawを一度正規化してexact hash/dense bandsを作ったが、hydrate後にunit float32をcanonical vectorとして返し、provenance再検査で同じ正規化関数へ再投入していた。このため正常vectorのhash/bucketが変わり、Memory/transcript exact queryの連続2回ともtargetは取得できても各回logical repairしていた。

## 実装

- raw vectorのdim/length/finite/nonzero検証とunit float32正規化を`_embedding_bands_batch`だけで一度行う。
- 新しい`_embedding_bands_from_normalised_batch`は検証済みunit bytesからexact hash/dense bandsだけを導出し、再正規化しない。
- sidecar build/queryは前者、canonical hydrate後のprovenance照合は後者を使う。これにより両者は同一unit表現を正本とする。
- schema generation、candidate/probe上限、repair回数、ACL/status、wrong-vector検出、source書込み境界は変更していない。

## RED → GREEN

- seed55 Memory exact: 連続2 queryでrepair 2のRED → target 2回取得・repair 0・source指紋不変。
- seed55 transcript exact: 同じRED → 同じGREEN。
- wrong-vector approximate/exact、ANN boundary/quality、不正vectorを含むfocused回帰は`10 passed`。
- fixed-seed 384次元1000組のnomination gateはcosine .72/.80/.90/.95/.99で956/992/1000/1000/1000を維持。

## 100k＋100k benchmark

実行: `.venv-test\Scripts\python.exe tools\benchmark_memory_retention.py --rounds 9`。一時DBのみ。

| 各source件数 | lexical p50/p95 ms | fast .99985 Mind | fast transcript | full .90 Mind | full transcript | build ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.427 / 18.220 | 8.789 / 10.328 | 4.320 / 5.626 | 105.806 / 113.140 | 46.634 / 58.716 | 1,982.140 |
| 10,000 | 1.952 / 5.883 | 9.311 / 10.105 | 4.523 / 5.693 | 158.104 / 173.024 | 58.822 / 61.605 | 20,481.218 |
| 100,000 | 2.174 / 6.821 | 9.541 / 10.374 | 5.399 / 6.115 | 358.708 / 367.410 | 145.127 / 163.909 | 208,847.395 |

- 100k fast候補Memory1/transcript1、full候補80/40、最終context各1/上限5。
- source Memory/transcriptは各規模before/after不変。Recall中/touch後restart rebuild 0/0。
- 100k sidecar 320,561,152 bytes、Python allocation peak 371,034,394 bytes、store reopen 5.754ms。
- full tierは14表×137 multiprobe、高衝突fixture、canonical provenance batch再導出を含む品質/integrityコスト。一般semantic latencyまたは本番SLOではない。
- 9 roundsは探索値。RAMは`tracemalloc`のPython allocationのみでOS RSS/SQLite/filesystem page cacheを含まない。reopenはembedder model loadを含まない。

## Verification

- 新規single-normalization test: `2 passed`。
- focused review回帰: `10 passed`。
- Stage M contract: `58 passed, 1 skipped in 18.38s`。
- Stage M＋Memory関連: `185 passed, 1 skipped in 44.26s`。
- py_compile: `store.py`、benchmark、contract test成功。
- 全pytest: `3072 passed, 2 skipped in 124.18s`。
- skipはWindows symlink privilege unavailable 2件。mock/path validationは実行済み。

## 変更ファイル

- `neuro_voice/mind/store.py`
- `tests/test_memory_retention_contract.py`
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

- test/benchmarkはpytest/tmpの一時DBだけを使用。canonical本番DB、persona、config、Trace、raw audio/imageへ非接触。
- 永続`memory.write_enabled`を変更せず、外部I/O・実機起動なし。
- git init/status/commit/reset/pushは実行していない。

## 未解決・後日確認事項

1. full tierの実モデル/実データ分布でのRecall/latency。今回の367.410ms p95は高衝突synthetic探索値。
2. 各100k＋100k build中のsource lock約209秒と約320.6MB sidecar。write-on運用ならsnapshot/online rebuildを別設計する。
3. 確認付き自然言語削除UXは未実装。storage primitiveとDO_NOT_STOREのみ維持。
4. transcript全文保持範囲、backup配置、disk監視、本番既存DB移行は別承認事項。
5. 同権限悪意writerの検出不能omissionとpath-name SQLite open TOCTOUの完全防御。

## 実機最小手順（別承認後）

1. 停止中の本番DBをbackupし、before件数・ID/本文/embedding指紋を取得。
2. 隔離コピーでgeneration 8 sidecarをbuildし、時間、OS RSS、disk、source lock影響を測る。
3. seed55相当の丸め敏感vector、fast/full相当の既知Memory/transcriptを2回ずつRecallし、repair 0、候補80/40、context5、persona/status漏れ0を確認。
4. sidecar明示rebuildと再起動後のrebuild 0、source件数・指紋不変を確認して本番昇格を判断。

## Rollback

- v6のStore/test/docsだけをv5へ戻せる。canonical source変更なし。
- generation 8 sidecarは停止中に派生ファイルだけ削除し、canonical DBから再構築可能。
- v6を戻すと一部正常float32 vectorで不要な毎query repairが再発するため、異常時はsemanticを無理に使わずlexical/recent fallbackへ退避する。

関連: PROJECT_CONSTITUTION第11・12・16・17・22条、D-045、ADR-0005、R-049。
