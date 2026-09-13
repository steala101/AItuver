# 2026-09-09 09:21 Sol — Stage M v3 final tuning

## 担当・範囲

- Stage M v3の独立レビュー2項目をTDDで再現し、ANN Recall品質、canonical vector validation、benchmark注記を修正した。
- Stage M以外、P3以降、本番DB/config/persona/過去Trace、実機、外部I/O、永続`memory.write_enabled`は変更していない。
- 既存v1/v2のwrite-disabled、source mutation rollback/read-only、freshness revision、ACL/status、sidecar repair/path safetyも再回帰した。

## REDと根本原因

1. 4表×16-bit/Hamming距離1のshared ANN nominationを384次元fixed-seed 1000組で測ると、cosine .72は268/1000、.80も受入率未達。別probeでもreviewと同じ低Recall傾向を再現した。cosine .90 seed 2は`Mind._recall_semantic`のMemory/transcript双方で0件だった。
   - 1表のHamming-1 hit確率が、`Mind`の低い受入境界まで覆えないことが根本原因。
2. sidecarをpoisonして不正canonical IDを候補化すると、dim mismatch、truncated float32、NaN、Inf、zero vectorがそのままMindのmatrixへ入り得た。
   - sidecarの`dim`だけをquery filterに使い、hydration後のcanonical blobを検証していなかった。
3. `run_benchmark(counts=(1000,), rounds=3)`でもreportに`rounds`がなく、注記が固定9だった。
4. 最初の7表/Hamming-3案はRecall 984/999/1000/1000/1000を満たしたが、1kのMind semantic p95が104.775msまで退行。697 bucket/表の大きなIN句が原因のため採用しなかった。

## 実装

- sidecar schema generationを8へ更新。
- full tierを14表×16-bit/Hamming距離2へ変更。各表のprobe bucketは137、返却sidecar rowは1280まで。
- 6表ずつのcompound queryにまとめ、SQLiteの保守的な999 bind上限を越えず、schema/freshness検査回数を減らした。
- high-similarity fast tierは先頭4表×Hamming距離1×各256行。hydration済みcanonical vectorとのcosineが`.95`以上の候補を実際に確認した場合だけfull tierを省略する。低・中類似度は必ずfull tierへ進む。
- final candidate上限はMemory 80、transcript 40、最終context各5のまま。全embedding scanは禁止したまま。
- canonical vectorはquery dim一致、`dim * 4` byte、finite、non-zeroを共通検証し、unit float32へ正規化した候補だけを返す。Memory/transcript双方へ適用。
- exact bucketは最終limitの4倍・最大1280だけ有界overfetchしてからcanonical検証し、最終80/40へ絞る。不正vector 90件を先頭、validを後方へ置くpoisoned sidecarでもvalidを保持した。
- benchmark reportへ実効`rounds`を追加し、percentile注記を実引数から生成。

## Recall品質

共有ANN nominationを384次元、seed=0..999の1000組で測定した。これは外部モデルを使わないdeterministic regression gate。

| cosine | hit / 1000 | gate |
|---:|---:|---:|
| .72 | 956 | >=950 |
| .80 | 992 | >=980 |
| .90 | 1000 | >=995 |
| .95 | 1000 | 1000 |
| .99 | 1000 | 1000 |

- cosine .90 seed 2の既知missは、production `Mind._recall_semantic`からMemory IDとtranscript IDを両方取得。
- 高cosine boundary、旧sampled bucket 300衝突後方、語彙違い、全件scan禁止も継続GREEN。
- nomination率は実データ精度や意味品質の一般保証ではなく、deterministic synthetic regression gate。

## 100k benchmark

実行: `.venv-test\\Scripts\\python.exe tools\\benchmark_memory_retention.py --rounds 9`。一時DBだけを使い、各規模でMemory/transcriptを同数、384次元synthetic vectorで作成した。

| 各source件数 | lexical p50/p95 ms | Mind semantic p50/p95 ms | transcript候補 p50/p95 ms | store reopen ms | build ms | Python alloc peak bytes | sidecar bytes |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 1.296 / 17.893 | 6.577 / 6.920 | 3.136 / 3.754 | 5.582 | 1,874.605 | 16,272,046 | 3,088,384 |
| 10,000 | 1.800 / 6.427 | 6.692 / 7.872 | 3.216 / 3.749 | 8.135 | 19,282.033 | 47,896,603 | 31,338,496 |
| 100,000 | 2.172 / 5.269 | 7.374 / 8.229 | 4.074 / 4.349 | 6.717 | 194,973.306 | 370,912,905 | 320,561,152 |

- Memory/transcript source件数は全規模で不変。候補limit/countは80/1、40/1、最終context各5/1。
- Recall中とtouch後restartの不要rebuildは0/0。
- query p95はv2の11.576msから8.229msへ改善したが、14表化で100k＋100kのbuildは65.934秒から194.973秒、sidecarは約150.9MBから約320.6MBへ増加した。
- buildは同じsource lockを全時間保持する。現write-off運用では初回/対象field更新時の既知Minorとして記録し、online snapshot rebuildの大改造は整合性リスクが高いため今回行っていない。
- 9 roundsは探索値で本番SLOではない。RAMは`tracemalloc`のPython allocationだけで、OS RSSとSQLite/filesystem page cacheを含まない。store reopenはembedder model loadを含まない。

## テスト

- Stage M contract: `45 passed, 1 skipped in 13.33s`。
- Stage M＋Memory関連: `119 passed, 1 skipped in 17.38s`。
- `py_compile`成功: `store.py`、`mind.py`、benchmark、contract test。
- 全pytest: `3059 passed, 2 skipped in 124.26s`。
- skipは2件ともWindows symlink privilege unavailable。mock/path validationは実行済み。

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

`mind.py`はv2のbounded Memory/transcript配線とstartup preload除去をそのまま使用し、v3では変更していない。

## 設定・DB・保護対象

- canonical DB schema migrationなし。generation 8は削除・再構築可能なsidecarだけ。
- config/persona/Trace/data、本番DB、永続write flagは変更なし。
- fault injectionとbenchmarkは一時DBだけ。外部I/O・実機起動なし。

## 未解決・確認事項

1. 100k＋100k sidecar build中の同一source lock約195秒と約320.6MB sidecar。write-off以外で運用する場合はsnapshot/online rebuildの別設計が必要。
2. 確認付き自然言語削除UXは未実装。storage primitiveとDO_NOT_STOREは維持。
3. transcript全文保持、backup配置、disk監視閾値、本番既存DB移行は所有者確認と別承認が必要。
4. fixed-seed synthetic Recall率は実モデル/実データの意味品質保証ではない。本番移行前に隔離コピーでdistributionを測る。
5. 悪意ある同directory同時writerとのpath-name SQLite open TOCTOU完全防御は未解決。directoryを信頼ownerだけに限定する。

## 実機最小手順（別承認後）

1. 停止中の本番DBをbackupし、before件数・ID/本文/embedding指紋を取得。
2. 隔離コピーでgeneration 8 sidecarをbuildし、時間、OS RSS、disk、source lock影響を測る。
3. cosine帯を含む既知Memory/transcriptを各1件以上Recallし、候補80/40、context5、persona/status漏れ0を確認。
4. 再起動後rebuild 0とsource件数・指紋不変を確認してから本番昇格を判断。

## Rollback

- v3の`store.py`、contract test、benchmark、同期docsだけをv2へ戻せる。canonical source変更はない。
- generation 8 sidecarは停止中に派生ファイルだけ削除可能で、canonical DBから再構築できる。
- 4表/Hamming-1へ戻すと今回の.72〜.95 Recall gateを再び破るため、本番rollback時はsemanticを無理に使わずlexical/recent fallbackへ退避する。

関連: PROJECT_CONSTITUTION第11・12・16・17・22条、D-045、ADR-0005、R-049。
