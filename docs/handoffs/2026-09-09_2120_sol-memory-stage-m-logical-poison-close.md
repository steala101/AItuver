# 2026-09-09 21:20 Sol — Stage M v4 logical-poison close

## 担当・範囲

- Stage M最終再レビューのImportant 1 / Minor 1をTDDと実動probeで修正した。
- 対象はlogical poisoned semantic sidecarとtier別benchmark表現だけ。P3以降、本番DB/config/persona/過去Trace、実機、外部I/O、永続`memory.write_enabled`は変更していない。
- v1〜v3の非削除、write-disabled、source mutation失敗境界、revision freshness、ANN Recall、canonical ACL/status/vector、sidecar破損/リンク安全性を再回帰した。

## REDと根本原因

1. approximate bucketの先頭をcanonical不正IDでMemory 80件またはtranscript 40件埋めると、ANN score後に`safe_limit`へ切ってからhydrateしていたため、後方のvalid cosine .90候補が消えた。
2. forged exact bucketがcanonical不正IDだけを返す場合、hydrate結果が空でも即returnしてfull ANNへ進まず、valid cosine .90候補を見失った。
3. v3 benchmarkの`semantic_query_*`はcosine .99985 fast tierだけを測り、Mindの受入範囲全体に見える名前だった。cosine .90 full tierの実コストは未表示だった。

## 実装

- Memory/transcript双方でsidecar候補IDとcanonical hydrate済みID集合を照合する。
- ID欠落、canonical vector不正、persona/scope/status不一致等を一件でも検出したら、canonicalへ触れず派生sidecar全体だけをdrop/rebuildし、同一queryを最大一回だけ再実行する。
- 再試行flagで無限修復を防止。再構築後もexact候補が全件invalidなら即returnせずbounded fast/full ANNへ進む。
- canonical不正vectorは既存のrebuild検証でsidecarへ再投入しない。正常sidecarでは追加probeもrebuildも発生しない。
- benchmarkはcosine .99985を`fast_*`、cosine .90を`full_*`としてMemory/transcript実経路、candidate上限/count、最終context上限/countを別記録する。rounds注記は実引数連動を維持。

## RED → GREEN

- `test_semantic_repair_prevents_invalid_approximate_rows_from_consuming_final_cap`
  - Memory80件先行、transcript40件先行の2 RED → 2 GREEN。
- `test_invalid_exact_sidecar_hit_repairs_once_then_falls_through_to_full_ann`
  - Memory/transcriptの2 RED → 2 GREEN。
- benchmark tier別key contractは旧実装でKeyError RED → GREEN。
- repair countはいずれも1。valid ID取得、invalid ID非返却を確認した。

## ANN品質（v3 gate再回帰）

fixed-seed 384次元1000組のshared nomination gateは維持した。

| cosine | hit / 1000 | gate |
|---:|---:|---:|
| .72 | 956 | >=950 |
| .80 | 992 | >=980 |
| .90 | 1000 | >=995 |
| .95 | 1000 | 1000 |
| .99 | 1000 | 1000 |

候補上限はMemory80/transcript40、context各5、内部row上限はfast 256/table・full 1280/tableのまま。毎turn全embedding scanは導入していない。

## 100k＋100k benchmark

実行: `.venv-test\Scripts\python.exe tools\benchmark_memory_retention.py --rounds 9`。一時DBだけを使い、各規模でMemory/transcriptを同数作成した。

| 各source件数 | lexical p50/p95 ms | fast .99985 Mind p50/p95 | fast transcript p50/p95 | full .90 Mind p50/p95 | full transcript p50/p95 | build ms |
|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.418 / 18.071 | 6.663 / 9.422 | 3.281 / 4.592 | 27.079 / 35.200 | 11.632 / 23.564 | 1,898.305 |
| 10,000 | 1.522 / 5.297 | 6.911 / 8.859 | 3.288 / 4.942 | 46.936 / 62.568 | 19.426 / 28.176 | 20,020.227 |
| 100,000 | 2.195 / 5.166 | 7.237 / 11.343 | 3.631 / 5.086 | 207.135 / 225.459 | 89.767 / 119.396 | 199,083.873 |

- 100kのfast候補はMemory1/transcript1、full候補は上限どおり80/40。最終contextはいずれも各1/上限5。
- Memory/transcript sourceは各規模でbefore/after不変。Recall中とtouch後restartの不要rebuildは0/0。
- 100k sidecarは320,561,152 bytes、`tracemalloc` peakは370,895,578 bytes、store reopenは5.859ms。
- full tierの大幅な差は、Recall gateを守る14表×137 multiprobeと、64種類のvectorを反復する高衝突fixtureでbounded上限までcandidate取得・hydrateするコスト。品質gateを緩めていない。これは一般semantic latencyや本番SLOではなく、9 roundsの探索値。
- RAM値はPython allocationだけでOS RSSとSQLite/filesystem page cacheを含まない。reopenはembedder model loadを含まない。

## Verification

- logical-poison focused: `4 passed`。
- Stage M contract: `49 passed, 1 skipped in 15.18s`。
- Stage M＋Memory関連: `176 passed, 1 skipped in 40.22s`。
- `py_compile`: `store.py`、benchmark、contract test成功。
- 全pytest: `3063 passed, 2 skipped in 125.51s`。
- skipは2件ともWindows symlink privilege unavailable。unit/path validationは実行済み。

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

- `config/config.yaml` mtimeは2026-08-07 02:22:12、data最新mtimeは2026-08-07 22:29:48、`logs/cognitive_trace.jsonl`は2026-08-07 22:28:14で不変。
- fault fixtureとbenchmarkはpytest/tmpの一時DBだけ。canonical本番DB、persona、config、Trace、raw audio/imageへ非接触。
- repository rootの`.git`には管理metadataがなく、git init/status/commit/resetは実行していない。

## 未解決・後日確認事項

1. 確認付き自然言語削除UXは未実装。storage primitiveとDO_NOT_STOREだけ維持し、破壊操作callerはユーザー承認後に別設計する。
2. full tierの実モデル/実データ分布でのRecall/latency。今回の225.459ms p95は高衝突synthetic探索値で、速度優先要件との最終tradeoff判断が必要。
3. 各100k＋100k build中の同一source lock約199秒と約320.6MB sidecar。write-on運用ならsnapshot/online rebuildを別設計する必要がある。
4. transcript全文保持範囲、backup配置、disk監視閾値、本番既存DB移行は所有者確認と別承認が必要。
5. 悪意ある同directory同時writerとのpath-name SQLite open TOCTOUは完全防御不能。directoryを信頼ownerだけに限定する。

## 実機最小手順（別承認後）

1. 停止中の本番DBをbackupし、before件数・ID/本文/embedding指紋を取得する。
2. 隔離コピーでgeneration 8 sidecarをbuildし、時間、OS RSS、disk、source lock影響を測る。
3. fast相当とcosine .72〜.95相当を含む既知Memory/transcriptをRecallし、候補80/40、context5、persona/status漏れ0、logical repair回数を確認する。
4. 再起動後rebuild 0とsource件数・指紋不変を確認してから本番昇格を判断する。

## Rollback

- v4の`store.py`、contract test、benchmark、同期docsだけをv3へ戻せる。canonical source変更はない。
- generation 8 sidecarは停止中に派生ファイルだけ削除し、canonical DBから再構築できる。
- logical repairを戻すとpoisoned candidateによるfalse-negativeが再発するため、異常時はsemanticを無理に使わずlexical/recent fallbackへ退避する。

関連: PROJECT_CONSTITUTION第11・12・16・17・22条、D-045、ADR-0005、R-049。
